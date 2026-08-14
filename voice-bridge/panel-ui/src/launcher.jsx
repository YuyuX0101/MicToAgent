import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import iconUrl from "./assets/card_icon.png";
import "./index.css";

// 与 voice-launcher.pyw 的四阶段一一对应
const STAGES = [
  { name: "启动桥接适配器", short: "适配器" },
  { name: "加载运行依赖库", short: "依赖库" },
  { name: "加载语音识别模型", short: "识别模型" },
  { name: "连接目标会话", short: "目标会话" },
];

const ICONS = {
  minus: "M5 12h14",
  close: ["M18 6 6 18", "M6 6l12 12"],
};

function Icon({ d, size = 13, strokeWidth = 2.2 }) {
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
      <Icon d={ICONS[kind]} />
    </button>
  );
}

const INITIAL = { stages: ["pending", "pending", "pending", "pending"], status: "正在初始化…", kind: "active" };

function Launcher() {
  const [snap, setSnap] = useState(INITIAL);

  useEffect(() => {
    let dead = false;
    // Python 侧主动推送（事件可能早于页面加载，故挂载后先拉一次快照对齐）
    window.launcherSync = (s) => setSnap(s);
    const pull = () => {
      window.pywebview.api
        .get_state()
        .then((s) => {
          if (!dead && s) setSnap(s);
        })
        .catch(() => {});
    };
    if (window.pywebview?.api) pull();
    else window.addEventListener("pywebviewready", pull, { once: true });
    return () => {
      dead = true;
      window.launcherSync = null;
    };
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") window.pywebview?.api?.win_close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const n = STAGES.length;
  let maxIdx = -1;
  snap.stages.forEach((s, i) => {
    if (s !== "pending") maxIdx = i;
  });
  const frac = maxIdx < 0 ? 0 : maxIdx / (n - 1);

  const statusCls =
    snap.kind === "done" ? "text-live" : snap.kind === "error" ? "text-danger" : "text-mist";

  return (
    <div className="flex h-full flex-col bg-page">
      <div className="flex h-9 shrink-0 items-center px-2">
        <div className="pywebview-drag-region h-full flex-1 cursor-default" />
        <WinBtn kind="minus" onClick={() => window.pywebview?.api?.win_minimize()} />
        <WinBtn kind="close" onClick={() => window.pywebview?.api?.win_close()} />
      </div>

      <div className="flex min-h-0 flex-1 flex-col items-center px-8 pb-6">
        <div className="pywebview-drag-region flex cursor-default flex-col items-center pt-3">
          <img src={iconUrl} alt="" draggable={false} className="h-14 w-14 rounded-2xl" />
          <div className="mt-2.5 text-[17px] font-semibold text-ink">MicToAgent</div>
          <div className="mt-0.5 text-[11.5px] text-faint">语音输入桥</div>
        </div>

        <div className={`mt-7 flex h-5 items-center text-[12.5px] ${statusCls}`}>
          {snap.kind === "active" && (
            <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-mist" />
          )}
          <span className="text-center leading-snug">{snap.status}</span>
        </div>

        {/* 节点串联进度：轨道对齐首末节点中心，填充缓动滑向下一节点 */}
        <div className="relative mt-8 w-full">
          <div className="absolute inset-x-8 top-0 flex h-3 items-center">
            <div className="relative h-[3px] w-full rounded-full bg-edge">
              <div
                className="absolute inset-y-0 left-0 rounded-full bg-ink transition-[width] duration-700 ease-fluid"
                style={{ width: `${frac * 100}%` }}
              />
            </div>
          </div>
          <div className="relative flex justify-between">
            {STAGES.map((s, i) => {
              const st = snap.stages[i];
              const dot =
                st === "error"
                  ? "bg-danger"
                  : i <= maxIdx
                    ? "bg-ink"
                    : "bg-edge";
              const label =
                st === "error"
                  ? "text-danger"
                  : st === "active"
                    ? "font-medium text-ink"
                    : st === "done"
                      ? "text-mist"
                      : "text-faint";
              return (
                <div key={s.short} className="flex w-16 flex-col items-center">
                  <span className="relative flex h-3 w-3 items-center justify-center">
                    {st === "active" && (
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ink/30" />
                    )}
                    <span
                      className={`relative inline-flex h-2.5 w-2.5 rounded-full transition-colors duration-300 ${dot}`}
                    />
                  </span>
                  <span className={`pt-2 text-[10.5px] transition-colors duration-300 ${label}`}>
                    {s.short}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="mt-auto flex w-full flex-col items-center">
          <div className="mb-3 h-px w-full bg-hairline" />
          <div className="text-[11px] text-faint">右Ctrl 点按常听 · 长按说话</div>
          <div className="pt-1 text-[11px] text-faint">控制面板切换发送方式 · 托盘右键退出</div>
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<Launcher />);
