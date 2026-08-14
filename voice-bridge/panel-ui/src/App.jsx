import { useEffect, useRef, useState } from "react";
import iconUrl from "./assets/card_icon.png";

// ---- 后端桥（pywebview js_api；所有取值/切换都由 Python 中转） ----
const api = () => window.pywebview.api;

const SEND_MODES = [
  { key: "fast", label: "快速发送", tip: "文字直达当前会话，不能配图。" },
  { key: "dialog", label: "对话框发送", tip: "粘贴到输入框并发送，可配图。" },
  { key: "compose", label: "输入对话框", tip: "只粘贴到输入框，由你检查后再发。" },
];

const CLOSE_ACTIONS = [
  { key: "tray", label: "最小化到托盘", caption: "点 X 收回托盘，语音继续工作" },
  { key: "quit", label: "退出进程", caption: "点 X 退出全部语音进程" },
];

const clip = (t, n) => {
  t = String(t).split(/\s+/).join(" ");
  return t.length <= n ? t : t.slice(0, n - 1) + "…";
};

// ---- 图标（lucide 风格线性图标） ----
function Icon({ d, size = 14, strokeWidth = 2 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {Array.isArray(d) ? d.map((p, i) => <path key={i} d={p} />) : <path d={d} />}
    </svg>
  );
}
const ICONS = {
  minus: "M5 12h14",
  close: ["M18 6 6 18", "M6 6l12 12"],
  back: "m15 18-6-6 6-6",
  gear: [
    "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z",
    "M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  ],
};

// ---- 分段选择器（cir-tabs 同款：云白胶囊轨道 + 墨色选中块） ----
let lastTipHide = 0; // 气泡组：一个刚关，下一个立即出，不再延迟

function SegTabs({ options, value, onChange }) {
  const [tipKey, setTipKey] = useState(null);
  const timer = useRef(null);

  const enter = (key, hasTip) => {
    clearTimeout(timer.current);
    if (!hasTip) return;
    const instant = Date.now() - lastTipHide < 250;
    timer.current = setTimeout(() => setTipKey(key), instant ? 0 : 420);
  };
  const leave = () => {
    clearTimeout(timer.current);
    if (tipKey) lastTipHide = Date.now();
    setTipKey(null);
  };

  return (
    <div className="flex w-full items-center gap-1 rounded-full border border-edge bg-cloud p-1.5 shadow-card">
      {options.map((o, i) => {
        const on = o.key === value;
        // 气泡定位：中间居中，两端贴边，避免被窗口边缘裁掉
        const tipPos =
          i === 0 ? "left-0" : i === options.length - 1 ? "right-0" : "left-1/2 -translate-x-1/2";
        return (
          <div
            key={o.key}
            className="relative min-w-0 flex-1"
            onMouseEnter={() => enter(o.key, !!o.tip)}
            onMouseLeave={leave}
          >
            <button
              onClick={() => onChange(o.key)}
              className={[
                "h-9 w-full rounded-full text-[13px] font-medium",
                "transition-[background-color,color,box-shadow,transform] duration-[220ms] ease-fluid",
                "active:scale-[0.97]",
                on ? "bg-ink text-cloud shadow-pill" : "text-mist hover:text-ink",
              ].join(" ")}
            >
              {o.label}
            </button>
            {tipKey === o.key && o.tip && (
              <div className={`pointer-events-none absolute bottom-full z-50 mb-2 ${tipPos}`}>
                <div className="tip-in whitespace-nowrap rounded-lg bg-ink px-2.5 py-1.5 text-[11.5px] font-medium text-cloud shadow-pill">
                  {o.tip}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---- 开关（与选中块同色的墨色轨道） ----
function Switch({ on, onClick }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={[
        "flex h-[22px] w-[38px] shrink-0 items-center rounded-full p-[3px]",
        "transition-colors duration-200 ease-fluid active:scale-95",
        on ? "bg-ink" : "bg-edge",
      ].join(" ")}
    >
      <span
        className={[
          "h-4 w-4 rounded-full bg-cloud shadow-sm",
          "transition-transform duration-200 ease-fluid",
          on ? "translate-x-4" : "translate-x-0",
        ].join(" ")}
      />
    </button>
  );
}

function SectionTitle({ children, center = false, className = "" }) {
  return (
    <div
      className={`pb-2.5 text-[11px] font-semibold tracking-[0.22em] text-mist ${
        center ? "text-center" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

function StatRow({ label, children }) {
  return (
    <div className="flex h-11 items-center justify-between px-4">
      <span className="text-[11.5px] tracking-[0.14em] text-faint">{label}</span>
      {children}
    </div>
  );
}

function WinBtn({ kind, onClick }) {
  return (
    <button
      onClick={onClick}
      className={[
        "flex h-7 w-8 items-center justify-center rounded-lg",
        "transition-colors duration-150 active:scale-95",
        kind === "close"
          ? "text-faint hover:bg-red-50 hover:text-danger"
          : "text-faint hover:bg-hairline hover:text-ink",
      ].join(" ")}
    >
      <Icon d={ICONS[kind]} size={13} strokeWidth={2.2} />
    </button>
  );
}

// ---- 主页 ----
function Home({ st, onMic, onMode, onSettings }) {
  const gate = st?.gate ?? "0";
  const live = gate !== "0";
  const title = gate === "1" ? "常听中" : gate === "2" ? "按住说话" : "静音";
  const sub =
    gate === "1" ? "对着麦克风说即可" : gate === "2" ? "松开即提交" : "点击开启收音";

  let session, sessionDim = false;
  if (st?.session_title) {
    session = clip(st.session_title, 24);
  } else if (st?.target === "draft") {
    session = "新建会话草稿页";
  } else {
    session = "未对准 Kimi Web";
    sessionDim = true;
  }
  const workspace = st?.workspace ? clip(st.workspace, 24) : "—";
  const ready = !!st?.ready;

  return (
    <div className="page-in flex min-h-0 flex-1 flex-col px-5 pb-4">
      <div className="pywebview-drag-region flex cursor-default flex-col items-center pb-4 pt-1">
        <img src={iconUrl} alt="" draggable={false} className="h-[52px] w-[52px] rounded-2xl" />
        <div className="mt-2 text-[17px] font-semibold text-ink">MicToAgent</div>
        <div className="mt-0.5 text-[11.5px] text-faint">语音输入桥</div>
      </div>

      <section className="flex items-center justify-between rounded-2xl border border-edge bg-cloud p-4 shadow-card">
        <div>
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              {live && (
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-live opacity-60" />
              )}
              <span
                className={`relative inline-flex h-2 w-2 rounded-full ${
                  live ? "bg-live" : "bg-faint"
                }`}
              />
            </span>
            <span className="text-[16px] font-semibold text-ink">{title}</span>
          </div>
          <div className="pl-4 pt-1 text-[11.5px] text-mist">{sub}</div>
        </div>
        <button
          onClick={onMic}
          className={
            live
              ? "h-9 rounded-full border border-edge bg-cloud px-4 text-[12.5px] font-medium text-mist transition-[color,transform] duration-200 ease-fluid hover:text-ink active:scale-[0.97]"
              : "h-9 rounded-full bg-ink px-4 text-[12.5px] font-medium text-cloud shadow-pill transition-[background-color,transform] duration-200 ease-fluid hover:bg-ink-hover active:scale-[0.97]"
          }
        >
          {live ? "静音" : "开启收音"}
        </button>
      </section>

      <SectionTitle center className="pt-5">
        发送模式
      </SectionTitle>
      <SegTabs options={SEND_MODES} value={st?.send_mode ?? "fast"} onChange={onMode} />

      <section className="mt-5 divide-y divide-hairline rounded-2xl border border-edge bg-cloud shadow-card">
        <StatRow label="工作区">
          <span
            className={`text-[12px] ${st?.workspace ? "text-ink" : "text-faint"}`}
          >
            {workspace}
          </span>
        </StatRow>
        <StatRow label="会话">
          <span className={`text-[12px] ${sessionDim ? "text-faint" : "text-ink"}`}>
            {session}
          </span>
        </StatRow>
        <StatRow label="链路">
          <span
            className={`flex items-center gap-1.5 text-[12px] ${
              ready ? "text-live" : "text-faint"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${ready ? "bg-live" : "bg-faint"}`}
            />
            {ready ? "就绪" : "未就绪"}
          </span>
        </StatRow>
      </section>

      <div className="mt-auto flex items-center justify-between pt-3">
        <button
          onClick={onSettings}
          aria-label="设置"
          className="rounded-lg p-1.5 text-faint transition-[color,transform] duration-150 hover:text-ink active:scale-95"
        >
          <Icon d={ICONS.gear} size={17} strokeWidth={1.8} />
        </button>
        <span className="text-[10.5px] text-faint">右Ctrl 点按常听 · 长按说话</span>
      </div>
    </div>
  );
}

// ---- 设置页 ----
function Settings({ st, autoErr, onBack, onAutostart, onSilent, onShowLauncher, onCloseAction }) {
  const prefs = st?.prefs ?? { close_action: "tray", silent_start: false, show_launcher: true };
  const autostart = !!st?.autostart;
  const caption =
    CLOSE_ACTIONS.find((a) => a.key === prefs.close_action)?.caption ?? "";

  return (
    <div className="page-in flex min-h-0 flex-1 flex-col px-5 pb-4">
      <div className="relative flex h-10 items-center">
        <button
          onClick={onBack}
          aria-label="返回"
          className="absolute left-0 flex h-7 w-7 items-center justify-center rounded-lg text-mist transition-[background-color,color,transform] duration-150 hover:bg-hairline hover:text-ink active:scale-95"
        >
          <Icon d={ICONS.back} size={15} strokeWidth={2.2} />
        </button>
        <div className="w-full text-center text-[13.5px] font-semibold text-ink">设置</div>
      </div>

      <SectionTitle className="pt-2">启动</SectionTitle>
      <section className="divide-y divide-hairline rounded-2xl border border-edge bg-cloud shadow-card">
        <div className="flex items-center justify-between px-4 py-3">
          <div>
            <div className="text-[13px] font-medium text-ink">开机自启</div>
            <div className="pt-0.5 text-[11px] text-faint">登录 Windows 后自动启动</div>
          </div>
          <Switch on={autostart} onClick={onAutostart} />
        </div>
        <div className="flex items-center justify-between px-4 py-3">
          <div>
            <div className="text-[13px] font-medium text-ink">静默启动</div>
            <div className="pt-0.5 text-[11px] text-faint">启动时不弹出面板窗口</div>
          </div>
          <Switch on={!!prefs.silent_start} onClick={onSilent} />
        </div>
        <div className="flex items-center justify-between px-4 py-3">
          <div>
            <div className="text-[13px] font-medium text-ink">启动进度卡</div>
            <div className="pt-0.5 text-[11px] text-faint">关闭后启动完全静默（失败仍会弹出）</div>
          </div>
          <Switch on={prefs.show_launcher !== false} onClick={onShowLauncher} />
        </div>
      </section>
      {autoErr && <div className="pt-2 text-[11px] text-danger">{autoErr}</div>}

      <SectionTitle className="pt-5">关闭窗口</SectionTitle>
      <SegTabs options={CLOSE_ACTIONS} value={prefs.close_action} onChange={onCloseAction} />
      <div className="pt-2.5 text-center text-[11px] text-faint">{caption}</div>
    </div>
  );
}

// ---- 根组件：状态轮询 + 动作分发 ----
export default function App() {
  const [st, setSt] = useState(null);
  const [page, setPage] = useState("home");
  const [autoErr, setAutoErr] = useState("");

  useEffect(() => {
    let dead = false;
    let timer;
    const tick = async () => {
      try {
        const s = await api().get_state();
        if (!dead) setSt(s);
      } catch {
        /* 桥还没就绪时下个周期再试 */
      }
      if (!dead) timer = setTimeout(tick, 800);
    };
    if (window.pywebview?.api) tick();
    else window.addEventListener("pywebviewready", tick, { once: true });
    return () => {
      dead = true;
      clearTimeout(timer);
    };
  }, []);

  const call = async (name, patch, ...args) => {
    // 乐观更新：先本地改状态让界面立刻响应，再等 Python 回包对齐
    if (patch) setSt((s) => (s ? { ...s, ...patch } : s));
    try {
      const s = await api()[name](...args);
      // 取值类动作直接返回最新状态，界面即时刷新
      if (s && typeof s === "object" && "send_mode" in s) setSt(s);
      return s;
    } catch {
      return null;
    }
  };

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (page === "settings") setPage("home");
      else call("win_close", null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [page]);

  const onAutostart = async () => {
    const r = await call("set_autostart", { autostart: !st?.autostart }, !st?.autostart);
    if (r && !r.ok) setAutoErr(r.error || "设置失败");
    else {
      setAutoErr("");
      call("get_state", null);
    }
  };

  return (
    <div className="h-full w-full">
      <div className="flex h-full flex-col overflow-hidden bg-page">
        <div className="flex h-9 shrink-0 items-center px-2">
          <div className="pywebview-drag-region h-full flex-1 cursor-default" />
          <WinBtn kind="minus" onClick={() => call("win_minimize", null)} />
          <WinBtn kind="close" onClick={() => call("win_close", null)} />
        </div>
        {page === "home" ? (
          <Home
            st={st}
            onMic={() => {
              const live = (st?.gate ?? "0") !== "0";
              call("set_gate", { gate: live ? "0" : "1" }, live);
            }}
            onMode={(m) => call("set_send_mode", { send_mode: m }, m)}
            onSettings={() => setPage("settings")}
          />
        ) : (
          <Settings
            st={st}
            autoErr={autoErr}
            onBack={() => setPage("home")}
            onAutostart={onAutostart}
            onSilent={() =>
              call(
                "set_silent_start",
                { prefs: { ...st?.prefs, silent_start: !st?.prefs?.silent_start } },
                !st?.prefs?.silent_start
              )
            }
            onShowLauncher={() =>
              call(
                "set_show_launcher",
                { prefs: { ...st?.prefs, show_launcher: !(st?.prefs?.show_launcher ?? true) } },
                !(st?.prefs?.show_launcher ?? true)
              )
            }
            onCloseAction={(a) =>
              call("set_close_action", { prefs: { ...st?.prefs, close_action: a } }, a)
            }
          />
        )}
      </div>
    </div>
  );
}
