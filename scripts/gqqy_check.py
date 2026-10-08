#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""高企查询 —— 高企认定「知识产权是否被报过」批量查重
（技能原名 gqqy-ip-used-check，2026-09-21 改名；脚本文件名保持不变）

原理（2026-09-21 实机验证）
--------------------------
高企认定系统**没有只读查重入口**（「查找」按钮只是刷新列表）。唯一判定信号来自
「添加知识产权」的保存响应：

  * 报过   -> 服务端回显红字「XX公司于YYYY年，使用该知识产权（ZL...）」，
              并**拒绝入库**（知识产权列表仍为 0 条）
  * 未报过 -> 记录成功写入「知识产权列表」（共 1 条），本脚本随即勾选删除，不留痕

关键接口 / 选择器（均在 innocomFrame 这个 iframe 内）
  GET  addDataEprIntellectualPropert.do        # 打开「添加」弹窗（也是保存的 action）
  POST addDataEprIntellectualPropert.do        # 「保存」-> 返回 saveOrUpdate 页面（含判定结果）
  POST findIpBySqh.do                          # 填专利号时异步校验：{msg:success|failure_null|failure, ip:{...}}
  POST batchDeleteDataEprIntellectualPropert.do # 列表删除
  #propertyNo / #propertyCategory / #propertyAccredit / #propertyName
  #propertyWay / #txtDate / #yyzzFile / #yyzzFileHid / #addsave
  判据 DOM：`.uploadAttachment h3`（红字横幅） / `#checkTip`（未检索到） / `#propertyNameyc`

★ 两个必须记住的坑
  1) bsk 的参数解析会在「引号内的空格」处截断表达式 —— 注入 JS 里 **禁止** 出现
     `' '` 或 `" "`（会报 SyntaxError: missing ) after argument list）。普通缩进空格无害。
  2) bsk upload 往 `#yyzzFile` 注入本地 PDF 会返回 CDP `Not allowed`（扩展权限所限），
     所以本脚本不传附件，而是把 `#propertyNameyc` 置成与 `#propertyName` 同值、
     并把 `#yyzzFileHid` 置非空，从而绕过 `#addsave` 里那两处
     `if(file==null||file==""){alert(...);return false;}`。

用法
----
  python gqqy_check.py setup  [--session S]                  # 登录/导航自检
  python gqqy_check.py check  --session S <专利号...>        # 批量查重 -> CSV
  python gqqy_check.py check  --session S --file nums.txt
  选项: --out 结果.csv   --no-clean    --record N   --json
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _find_bsk():
    """定位 bsk 可执行文件：环境变量 BSK_BIN -> PATH -> 常见安装位置。

    不写死任何机器的用户名，方便直接拷给别人用。
    """
    env = os.environ.get("BSK_BIN")
    if env and os.path.exists(env):
        return env
    for name in ("bsk", "bsk.exe", "bsk.cmd", "bsk.bat"):
        w = shutil.which(name)
        if w:
            return w
    cands = [
        os.path.expanduser("~/.local/bin/bsk.exe"),
        os.path.expanduser("~/.local/bin/bsk"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "bsk", "bsk.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "bsk.cmd"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


BSK = _find_bsk()
HOME_URL = "https://gqqy.chinatorch.org.cn/xonlinereport/homePage.do"
LOGIN_URL = "https://hjrz.chinatorch.org.cn/login"

DEFAULT_SESSION = "gqqy"

# 注入用 JS 片段（全部零"引号内空格"）
FIND = ("const findDlg=()=>{let fr=null;const walk=w=>{try{const d=w.document;"
        "if(d&&d.getElementById&&d.getElementById('addForm'))fr=w;}catch(e){}"
        "for(let i=0;i<w.frames.length;i++){walk(w.frames[i]);}};walk(window);return fr;};")
FIND_LIST = ("const findList=()=>{let fr=null;const walk=w=>{try{const d=w.document;"
             "const t=d.body?d.body.innerText:'';"
             "if(t.indexOf('知识产权列表')>=0&&d.forms&&d.forms.DataEprIntellectualPropert_list)fr=w;"
             "}catch(e){}for(let i=0;i<w.frames.length;i++){walk(w.frames[i]);}};"
             "walk(window);return fr;};")

# 专利号第 5 位 -> 高企系统的「类别」select 值
PLB_BY_KIND = {"1": "12", "2": "2", "3": "3", "4": "4"}
PLB_NAME = {"12": "发明专利（非国防专利）", "2": "实用新型专利", "3": "外观设计专利", "4": "软件著作权"}


# ---------------------------------------------------------------- bsk 封装
def bsk(args, timeout=240, env_extra=None):
    if not BSK:
        return "bsk-not-found"
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run([BSK] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except FileNotFoundError:
        return "bsk-not-found"
    except subprocess.TimeoutExpired:
        return "bsk-timeout"
    return (p.stdout or "") + (p.stderr or "")


def preflight():
    """开跑前先确认 bsk 可用，避免跑到一半才炸。返回 None 表示正常。

    ★ 先做一次「不自动拉起 daemon」的快速探活（BSK_AUTO_START=0，25s 上限）。
    daemon 已经死掉时，带自动启动的 `bsk status` 会**一直挂着不返回**（本机实测 >90s），
    表现为「脚本完全没有输出、像卡死」，实际是在等一个永远起不来的 daemon。
    探活失败要立刻给出一条能照做的恢复指令，而不是让用户干等。
    """
    if not BSK:
        return ("未找到 bsk 可执行文件。\n"
                "  bsk 由 browser-skill 提供，请先安装它，或用环境变量指定路径：\n"
                '  set BSK_BIN=D:\\path\\to\\bsk.exe   （PowerShell: $env:BSK_BIN="..."）')

    st = bsk(["status", "--json"], timeout=25, env_extra={"BSK_AUTO_START": "0"})
    if "bsk-timeout" in st or "bsk-not-found" in st or not st.strip():
        return ("bsk daemon 探活超时（25s 无返回）。\n"
                "  daemon 已死时，带自动启动的 bsk status 会一直挂着不返回。\n"
                "  处理：先把 `bsk daemon start --foreground` 放进一个**能常驻的后台任务**里跑，\n"
                "  再带 BSK_AUTO_START=0 重跑本脚本。")
    if "automatic daemon startup is disabled" in st or '"daemon_version"' not in st:
        return ("bsk daemon 没有在运行。\n"
                "  处理：`bsk daemon start --foreground`（必须放在常驻后台任务里，\n"
                "  命令一结束进程会被回收），然后带 BSK_AUTO_START=0 重跑本脚本。")
    try:
        j = json.loads(st[st.find("{"):])
    except Exception:
        j = {}
    if not j.get("browsers"):
        return ("bsk daemon 在跑，但**没有连上浏览器**。\n"
                "  请确认浏览器里 browser-skill 扩展已启用，用 `bsk browsers` 检查。")
    return None


def bsk_json(args, timeout=240):
    out = bsk(args + ["--json"], timeout)
    i = out.find("{")
    if i < 0:
        return {"_raw": out}
    try:
        return json.loads(out[i:])
    except Exception:
        return {"_raw": out}


def evaluate(session, expr, timeout_s="90s"):
    assert "' '" not in expr and '" "' not in expr, \
        "注入 JS 不能含引号内空格（会被 bsk 截断）"
    r = bsk_json(["evaluate", "--session", session, "--timeout", timeout_s, expr])
    if r.get("ok") is False:
        return {"_jserr": r.get("error")}
    return r.get("value")


def observe(session):
    return bsk(["observe", "--session", session])


def ref_of(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None


# ---------------------------------------------------------------- 数据归一化
def normalize(sqh):
    """2024229404464 -> ZL202422940446.4；已带 ZL/点号也能处理。非 13 位返回 None。

    ★ 末位可以是 X（校验位 = 10 时 CNIPA 用 X 表示），所以判据是
      `^[0-9]{12}[0-9X]$` 而不是 `isdigit()`（旧版用 isdigit 会把 X 号误判格式错误）。
    """
    s = re.sub(r"[^0-9A-Za-z]", "", str(sqh)).upper()
    if s.startswith("ZL"):
        s = s[2:]
    if len(s) == 13 and re.match(r"^[0-9]{12}[0-9X]$", s):
        return "ZL" + s[:-1] + "." + s[-1]
    return None


def check_digit_ok(sqh):
    """校验位自检：前 12 位按权重 2..9,2..5 加权求和 %11，10 记作 X。

    返回 (True, 期望末位) / (False, 期望末位)。用来挡住「手抄掉一位/写错一位」
    被当成「未报过」的假阴性——那是本技能最危险的交付事故。
    """
    s = re.sub(r"[^0-9A-Za-z]", "", str(sqh)).upper()
    if s.startswith("ZL"):
        s = s[2:]
    if len(s) != 13 or not re.match(r"^[0-9]{12}[0-9X]$", s):
        return None, None
    w = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    r = sum(int(c) * k for c, k in zip(s[:12], w)) % 11
    want = "X" if r == 10 else str(r)
    return (want == s[12], want)


def category_of(sqh):
    # 保留末位 X，否则 X 号会被缩成 12 位而误落到默认值「实用新型」
    s = re.sub(r"[^0-9X]", "", str(sqh).upper())
    return PLB_BY_KIND.get(s[4], "2") if len(s) == 13 else "2"


# ---------------------------------------------------------------- 会话 / 导航
def list_sessions():
    txt = bsk(["session", "list", "--json"], timeout=90)
    j = txt.find("[")
    if j < 0:
        return []
    try:
        arr = json.loads(txt[j:])
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def session_ok(session):
    return any(s.get("session_id") == session for s in list_sessions())


def start_session(name=None):
    """新建会话，返回真实的 session_id。

    注意：bsk 的 session_id 是随机四字母，`--name` 只是显示名；`session start`
    **没有** `--session` 参数（写错会直接报参数缺失）。
    """
    args = ["session", "start", "--no-focus", "--json"]
    if name:
        args += ["--name", str(name)]
    out = bsk(args, timeout=180)
    i = out.find("{")
    if i >= 0:
        try:
            sid = json.loads(out[i:]).get("session_id")
            if sid:
                return sid
        except Exception:
            pass
    time.sleep(2)
    ss = list_sessions()
    return ss[-1]["session_id"] if ss else None


def ensure_session(want, log=print):
    """优先复用已有会话，避免每跑一次就新开一个 Agent Window。"""
    ids = [s.get("session_id") for s in list_sessions() if s.get("session_id")]
    if want and want in ids:
        return want
    if len(ids) == 1:
        if want:
            log("（未找到会话 %s，复用现有会话 %s）" % (want, ids[0]))
        return ids[0]
    if len(ids) > 1:
        log("（检测到多个会话 %s，使用 %s）" % (ids, ids[-1]))
        return ids[-1]
    sid = start_session(want)
    if sid:
        log("（新建会话 %s，窗口稍后就绪）" % sid)
    return sid or want


def gqqy_ready(txt):
    """当前观察结果是否表示「已登录且落在高企系统外壳里」。"""
    return ("退出登录" in txt) or bool(ref_of(txt, r'(@e\d+) link "高企认定申报"'))


def on_sso_portal(txt):
    """是否停在「统一身份认证与单点登录平台」门户页。

    ★ 新建会话（没有可复用的旧会话）时，Agent Window 打开的就是这个门户，
      而不是高企系统页——必须先经门户的「我要办理」才能进高企系统。
    """
    return "统一身份认证与单点登录平台" in txt and "我要办理" in txt


def looks_like_login_page(txt):
    """真正的登录页判据。

    ★ 绝不能只看「登录」二字：门户页标题「统一身份认证与单点**登录**平台」
      里就含「登录」，用它当判据会把已登录状态误报成未登录（2026-09-21 实测踩过）。
      这里改看登录页特有的元素。
    """
    if on_sso_portal(txt) or "注销" in txt:
        return False
    t = txt.lower()
    return ("忘记密码" in txt or "立即注册" in txt or "验证码" in txt
            or "password" in t or "账号登录" in txt or "用户登录" in txt)


def _portal_enter(session, txt, log=print):
    """在统一身份认证门户页点「我要办理」进高企系统，返回点完之后的最新 observe 文本。

    入口那一行的 ctx 实际是「高新技术企业认定 / 先进制造业企业增值税加计抵减政策申报」，
    所以不能写死整串；这里按前缀匹配，再兜一个含「高新技术企业」的宽松匹配。
    """
    r = (ref_of(txt, r'(@e\d+) link "我要办理" \[ctx: 高新技术企业认定')
         or ref_of(txt, r'(@e\d+) link "我要办理" \[ctx: [^\]]*高新技术企业'))
    if not r:
        return txt
    log("当前在统一身份认证门户，从「我要办理」进入高企系统…")
    bsk(["click", r, "--session", session], timeout=120)
    for _ in range(15):
        time.sleep(2)
        txt = observe(session)
        if gqqy_ready(txt):
            break
    return txt


def goto_ip_table(session, record=1, log=print):
    """确保当前页面停在「知识产权汇总表」。

    返回 {'ok':True} 或 {'ok':False,'need':'login'|'manual','msg':...}
    """
    txt = observe(session)
    if 'button "添加"' in txt and "知识产权列表" in txt:
        log("已在知识产权汇总表页")
        return {"ok": True, "where": "already"}

    # ★ 走两轮：新会话首次 observe 时门户 iframe 常常还没渲染出「我要办理」，
    #   于是第一轮认不出门户、跑去 navigate 首页被弹回，第二轮才能认出并点进去。
    #   一轮就判失败会把「页面还没加载完」误报成「需人工介入」（2026-09-21 实测踩过）。
    for attempt in (1, 2):
        # A) 新会话常落在统一身份认证门户 -> 走「我要办理」进高企系统
        if on_sso_portal(txt):
            txt = _portal_enter(session, txt, log)
            if gqqy_ready(txt):
                break

        # B) 仍不在高企系统页 -> 直接跳首页
        #    注意：navigate 偶尔会撞上 bsk 内部 30s RPC 上限而报超时，
        #    但页面往往已经跳成功；下一轮 observe 会重新判断，不必当致命错误。
        if not gqqy_ready(txt):
            bsk(["navigate", HOME_URL, "--session", session], timeout=180)
            for _ in range(10):      # 最多等 ~20s：新开的会话首次加载明显更慢
                time.sleep(2)
                txt = observe(session)
                if gqqy_ready(txt):
                    break
        if gqqy_ready(txt):
            break
        if attempt == 1:
            time.sleep(3)           # 给门户 iframe 一点时间把入口渲染出来
            txt = observe(session)

    if not gqqy_ready(txt):
        if looks_like_login_page(txt):
            return {"ok": False, "need": "login",
                    "msg": "未检测到登录态，请先在浏览器里登录 " + LOGIN_URL}
        if on_sso_portal(txt):
            return {"ok": False, "need": "manual",
                    "msg": "已登录、但停在统一身份认证门户且没找到「高新技术企业认定」的"
                           "「我要办理」入口，请人工点一下 " + LOGIN_URL}
        return {"ok": False, "need": "manual",
                "msg": "高企系统首页没加载出来（或页面结构变了），请人工打开 " + HOME_URL + " 看一眼"}

    # 首页 -> 高企认定申报
    r = ref_of(txt, r'(@e\d+) link "高企认定申报"')
    if not r:
        return {"ok": False, "need": "manual", "msg": "找不到「高企认定申报」入口"}
    bsk(["click", r, "--session", session])
    time.sleep(5)
    txt = observe(session)

    # 申报材料列表 -> 第 N 条「修改」（按 rows 顺序，1 = 最上面那条）
    mods = re.findall(r'(@e\d+) link "修改', txt)
    if not mods:
        return {"ok": False, "need": "manual", "msg": "申报材料列表里没有可编辑的记录"}
    if record > len(mods):
        return {"ok": False, "need": "manual",
                "msg": f"只找到 {len(mods)} 条可编辑记录（可用 --record 指定 1..{len(mods)}）"}
    bsk(["click", mods[record - 1], "--session", session])
    time.sleep(6)
    txt = observe(session)

    # 申报表单 -> 知识产权汇总表
    r = ref_of(txt, r'(@e\d+) link "知识产权汇总表"')
    if not r:
        return {"ok": False, "need": "manual", "msg": "申报表单里找不到「知识产权汇总表」页签"}
    bsk(["click", r, "--session", session])
    time.sleep(5)
    txt = observe(session)
    if 'button "添加"' not in txt:
        return {"ok": False, "need": "manual", "msg": "「知识产权汇总表」未加载出列表"}
    log("已进入知识产权汇总表")
    return {"ok": True, "where": "navigated"}


# ---------------------------------------------------------------- 页面状态
def state(session, psqh):
    js = ("(()=>{const out=[];const walk=w=>{try{const d=w.document;"
          "const t=d.body?d.body.innerText:'';"
          "if(t.indexOf('知识产权编号')>=0||t.indexOf('知识产权列表')>=0){"
          "out.push({url:String(d.location.href),"
          "hasUsed:t.indexOf('使用该知识产权')>=0,"
          "hasAddForm:!!d.getElementById('addForm'),"
          "psqhHit:t.indexOf('" + psqh + "')>=0,"
          "cnt:(t.match(/共\\d+条/g)||[]),"
          "body:t.replace(/\\s+/g,'').slice(0,900)});}}catch(e){out.push({err:String(e)});}"
          "for(let i=0;i<w.frames.length;i++){walk(w.frames[i]);}};walk(window);return out;})()")
    r = evaluate(session, js)
    return r if isinstance(r, list) else [{"_err": str(r)[:300]}]


def close_dialog(session):
    r = ref_of(observe(session), r'(@e\d+) button "返回"')
    if r:
        bsk(["click", r, "--session", session])
        time.sleep(3)
        return True
    return False


def cleanup(session):
    """勾选「知识产权列表」中的全部行并删除（探针只留 1 行）"""
    js = ("(()=>{" + FIND_LIST +
          "const F=findList();if(!F){return{err:'nolist'};}const d=F.document;"
          "const cbs=d.querySelectorAll('input[type=checkbox][name=items]');"
          "for(let i=0;i<cbs.length;i++){cbs[i].checked=true;}return{checked:cbs.length};})()")
    r = evaluate(session, js)
    if not isinstance(r, dict) or not r.get("checked"):
        return {"deleted": False, "reason": r}
    ref = ref_of(observe(session), r'(@e\d+) button "删除"')
    if not ref:
        return {"deleted": False, "reason": "找不到删除按钮"}
    bsk(["click", ref, "--session", session])
    time.sleep(4)
    cnts = ["?"]
    for f in state(session, "____"):
        if isinstance(f.get("cnt"), list) and f["cnt"]:
            cnts = f["cnt"]
            break
    return {"deleted": cnts[0] == "共0条", "remaining": cnts}


# ---------------------------------------------------------------- 单条探针
def probe(session, raw_sqh, do_clean=True, log=print):
    out = {"input": str(raw_sqh)}
    psqh = normalize(raw_sqh)
    if not psqh:
        out.update(status="格式错误", note="不是 13 位中国专利申请号")
        return out
    out["psqh"] = psqh
    out["plb"] = category_of(raw_sqh)

    ok, want = check_digit_ok(raw_sqh)
    if ok is False:
        out.update(status="格式错误",
                   note="校验位不符：按前 12 位应为 %s，实际给的是 %s（请核对号码是否抄错）"
                        % (want, psqh[-1]))
        return out

    if any(f.get("hasAddForm") for f in state(session, psqh)):
        close_dialog(session)

    add_ref = ref_of(observe(session), r'(@e\d+) button "[^"]*添加"')
    if not add_ref:
        out.update(status="需人工介入", note="不在知识产权汇总表页")
        return out
    bsk(["click", add_ref, "--session", session])
    time.sleep(3)

    fill = ("(async()=>{" + FIND +
            "const F=findDlg();if(!F){return{err:'nodialog'};}"
            "F.$('#propertyCategory').val('" + out["plb"] + "');"
            "F.$('#propertyAccredit').val('" + psqh + "');"
            "F.$('#propertyNo').val('01');"
            "F.$('#propertyName').val('探针校验用记录');"
            "F.$('#propertyWay').val('2');"
            "F.$('#txtDate').val('2020-01-01');"
            "F.flag=null;F.findIpBySqh();"
            "await new Promise(r=>{const t0=Date.now();const iv=setInterval(()=>{"
            "if((F.flag!==1&&F.flag!=null)||Date.now()-t0>20000){clearInterval(iv);r();}},300);});"
            "F.$('#yyzzFileHid').val('probe.pdf');"
            "F.$('#propertyNameyc').val(F.$('#propertyName').val());"
            "return{flag:String(F.flag)};})()")
    fr = evaluate(session, fill)
    if not isinstance(fr, dict) or fr.get("err"):
        out.update(status="需人工介入", note="添加弹窗未就绪，%s" % (fr,))
        return out
    # 佰腾侧校验码：2/3=佰腾无该专利数据  4=权属人非本企业  0=无效  99=正常
    out["baiTengFlag"] = fr.get("flag")

    save_ref = ref_of(observe(session), r'(@e\d+) button "保存"')
    if not save_ref:
        out.update(status="需人工介入", note="找不到保存按钮")
        return out
    click_out = bsk(["click", save_ref, "--session", session])
    out["_dialog"] = [l.strip() for l in click_out.splitlines() if "dialog:" in l]

    frames, verdict = [], None
    for _ in range(14):
        time.sleep(2)
        frames = state(session, psqh)
        used = [f for f in frames if f.get("hasUsed")]
        if used:
            verdict = ("used", used[0])
            break
        hit = [f for f in frames if isinstance(f.get("cnt"), list) and f["cnt"]
               and f["cnt"][0] != "共0条" and f.get("psqhHit")]
        if hit:
            verdict = ("clean", hit[0])
            break
        if not any(f.get("hasAddForm") for f in frames):
            break

    if verdict and verdict[0] == "used":
        b = re.sub(r"^.*?号为必填项", "", verdict[1]["body"])
        m = re.match(r"(.{2,80}?)于(\d{4})年", b)
        out["status"] = "报过"
        out["company"] = m.group(1).strip() if m else ""
        out["year"] = m.group(2) if m else ""
        out["raw"] = b[:200]
        close_dialog(session)
    elif verdict and verdict[0] == "clean":
        out["status"] = "未报过"
        out["cleanup"] = cleanup(session) if do_clean else {"deleted": None}
    else:
        out.update(status="未知", raw=str(frames)[:400])
        if any(f.get("hasAddForm") for f in frames):
            close_dialog(session)
    out.pop("_dialog", None)
    log("  %-16s -> %s %s" % (psqh, out["status"],
                             (out.get("company") or "") + (out.get("year") or "")))
    return out


# ---------------------------------------------------------------- CLI
def cmd_setup(a):
    a.session = ensure_session(a.session)
    r = goto_ip_table(a.session, a.record)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if r.get("ok"):
        frames = state(a.session, "____")
        for f in frames:
            if isinstance(f.get("cnt"), list) and f["cnt"]:
                print("当前列表计数:", f["cnt"])
                break
    return 0 if r.get("ok") else 2


def cmd_check(a):
    nums = list(a.numbers or [])
    if a.file:
        with open(a.file, encoding="utf-8-sig") as fh:
            for line in fh:
                for tok in re.split(r"[\s,;，；]+", line.strip()):
                    if tok:
                        nums.append(tok)
    if not nums:
        print("没有输入专利号", file=sys.stderr)
        return 1

    a.session = ensure_session(a.session)
    r = goto_ip_table(a.session, a.record)
    if not r.get("ok"):
        print(json.dumps(r, ensure_ascii=False, indent=1), file=sys.stderr)
        return 2

    results = [probe(a.session, n, not a.no_clean, log=print) for n in nums]

    rows = []
    for i, x in enumerate(results, 1):
        rows.append({
            "序号": i,
            "专利号": x.get("psqh") or x.get("input", ""),
            "是否报过": {"报过": "是", "未报过": "否"}.get(x.get("status"), ""),
            "使用公司": x.get("company", ""),
            "使用年份": x.get("year", ""),
            "类别": PLB_NAME.get(x.get("plb", ""), ""),
            "处理状态": x.get("status", ""),
            "备注": x.get("note", "") or ("" if x.get("cleanup", {}).get("deleted") in (True, None)
                                          else "清理未确认: %s" % (x.get("cleanup"),)),
        })
    out = a.out or ("高企知识产权查重结果_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("\n已写出: %s" % out)
    hit = sum(1 for r_ in rows if r_["是否报过"] == "是")
    print("合计 %d 条：报过 %d 条，未报过 %d 条，未判定 %d 条"
          % (len(rows), hit, sum(1 for r_ in rows if r_["是否报过"] == "否"),
             sum(1 for r_ in rows if not r_["是否报过"])))
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser(description="高企认定 知识产权报过查重")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="登录/导航自检")
    p.add_argument("--session", default=DEFAULT_SESSION)
    p.add_argument("--record", type=int, default=1, help="用第几条申报材料（1=最上面）")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("check", help="批量查重")
    p.add_argument("numbers", nargs="*", help="专利号（13位申请号或 ZL...格式）")
    p.add_argument("--session", default=DEFAULT_SESSION)
    p.add_argument("--file", help="从文本文件读专利号（空格/逗号/换行分隔）")
    p.add_argument("--out", help="CSV 输出路径")
    p.add_argument("--record", type=int, default=1)
    p.add_argument("--no-clean", action="store_true", help="未报过的记录不删除（危险）")
    p.add_argument("--json", action="store_true", help="额外打印完整 JSON")
    p.set_defaults(func=cmd_check)

    a = ap.parse_args()
    if getattr(a, "cmd", None) == "check":
        a.numbers = getattr(a, "numbers", [])

    err = preflight()
    if err:
        print("[环境检查未通过]\n" + err, file=sys.stderr)
        return 3
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
