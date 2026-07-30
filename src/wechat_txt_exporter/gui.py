from __future__ import annotations

import contextlib
import ctypes
import os
import queue
import threading
from datetime import datetime
from pathlib import Path

from .adapter import Weixin411Adapter
from .discovery import (
    discover_accounts,
    find_data_root,
    find_weixin_executable,
    verify_supported_version,
)
from .errors import ExporterError
from .exporter import export_all
from .key_recovery import recover_database_key
from .models import Account
from .voice import ModelLoadProgress


def _enable_high_dpi() -> None:
    """Prevent Windows from bitmap-scaling Tk, which makes the UI look blurry."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class _QueueWriter:
    def __init__(self, events: queue.Queue[tuple[str, object]]):
        self.events = events
        self.pending = ""

    def write(self, value: str) -> int:
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.events.put(("log", line))
        return len(value)

    def flush(self) -> None:
        if self.pending.strip():
            self.events.put(("log", self.pending))
        self.pending = ""


def _format_bytes(value: int) -> str:
    amount = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024.0 or unit == "GB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{amount:.1f} GB"


class ExporterWindow:
    def __init__(self) -> None:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import filedialog, ttk

        _enable_high_dpi()
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.root = tk.Tk()
        self.root.title("微信聊天记录 TXT 导出工具")
        dpi = max(96.0, float(self.root.winfo_fpixels("1i")))
        ui_scale = min(2.5, dpi / 96.0)
        window_width = int(900 * ui_scale)
        window_height = int(680 * ui_scale)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(window_width, int(screen_width * 0.9))
        window_height = min(window_height, int(screen_height * 0.88))
        left = max(0, (screen_width - window_width) // 2)
        top = max(0, (screen_height - window_height) // 2)
        self.root.geometry(f"{window_width}x{window_height}+{left}+{top}")
        self.root.minsize(int(780 * ui_scale), int(570 * ui_scale))
        self.root.tk.call("tk", "scaling", dpi / 72.0)
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            font = tkfont.nametofont(font_name)
            font.configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TButton", padding=(12, 7))
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.accounts: dict[str, Account] = {}
        self.verified_keys: dict[str, bytes] = {}
        self.working = False
        self.hook_prompted = False
        self.cancel_event = threading.Event()
        self.close_requested = False

        self.account_value = tk.StringVar()
        self.output_value = tk.StringVar(
            value=str(Path(__file__).resolve().parents[2] / "exports")
        )
        self.voice_value = tk.BooleanVar(value=True)
        self.voice_model_value = tk.StringVar(
            value=os.environ.get("WECHAT_VOICE_MODEL", "small")
        )
        self.phase_value = tk.StringVar(value="正在检测微信环境……")
        self.model_progress_value = tk.StringVar()

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(50, self._load_environment)
        self.root.after(100, self._drain_events)

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="微信聊天记录导出", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="微信 4.1.x · 每个好友或群聊生成一个 UTF-8 TXT",
            foreground="#555555",
        ).pack(anchor="w", pady=(3, 16))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        ttk.Label(form, text="目标账号", width=10).grid(row=0, column=0, sticky="w", pady=5)
        self.account_box = ttk.Combobox(form, textvariable=self.account_value, state="readonly")
        self.account_box.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=5)
        self.refresh_button = ttk.Button(form, text="刷新", command=self._load_environment)
        self.refresh_button.grid(row=0, column=2, pady=5)

        ttk.Label(form, text="输出目录", width=10).grid(row=1, column=0, sticky="w", pady=5)
        self.output_entry = ttk.Entry(form, textvariable=self.output_value)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=5)
        self.browse_button = ttk.Button(form, text="浏览…", command=self._browse)
        self.browse_button.grid(row=1, column=2, pady=5)
        ttk.Label(form, text="语音消息", width=10).grid(row=2, column=0, sticky="w", pady=5)
        voice_options = ttk.Frame(form)
        voice_options.grid(row=2, column=1, columnspan=2, sticky="w", pady=5)
        self.voice_check = ttk.Checkbutton(
            voice_options, text="转成文字并写入 TXT", variable=self.voice_value
        )
        self.voice_check.pack(side="left")
        ttk.Label(voice_options, text="识别模型：").pack(side="left", padx=(18, 5))
        self.voice_model_box = ttk.Combobox(
            voice_options,
            textvariable=self.voice_model_value,
            values=("tiny", "base", "small", "medium", "large-v3"),
            width=11,
            state="normal",
        )
        self.voice_model_box.pack(side="left")
        form.columnconfigure(1, weight=1)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(14, 10))
        self.verify_button = ttk.Button(actions, text="仅验证密钥", command=lambda: self._start(False))
        self.verify_button.pack(side="left")
        self.export_button = ttk.Button(actions, text="一键导出全部 TXT", command=lambda: self._start(True))
        self.export_button.pack(side="left", padx=10)

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(2, 6))
        ttk.Label(status, text="当前状态：").pack(side="left")
        ttk.Label(status, textvariable=self.phase_value, foreground="#1261a0").pack(side="left")

        self.model_progress_frame = ttk.Frame(outer)
        ttk.Label(
            self.model_progress_frame,
            textvariable=self.model_progress_value,
            foreground="#444444",
        ).pack(anchor="w", pady=(0, 4))
        self.model_progress_bar = ttk.Progressbar(
            self.model_progress_frame,
            mode="determinate",
            maximum=100,
        )
        self.model_progress_bar.pack(fill="x")

        self.log_frame = ttk.Frame(outer)
        self.log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            self.log_frame,
            height=17,
            state="disabled",
            wrap="word",
            font=("Consolas", 10),
            background="#fafafa",
        )
        scrollbar = ttk.Scrollbar(
            self.log_frame, orient="vertical", command=self.log.yview
        )
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("success", foreground="#16713b")
        self.log.tag_configure("error", foreground="#b42318")
        self.log.tag_configure("phase", foreground="#1261a0")
        self.log.tag_configure("muted", foreground="#666666")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.model_progress_frame.pack_forget()

    def _show_model_progress(self) -> None:
        if not self.model_progress_frame.winfo_manager():
            self.model_progress_frame.pack(
                fill="x", pady=(0, 8), before=self.log_frame
            )

    def _hide_model_progress(self) -> None:
        self.model_progress_bar.stop()
        self.model_progress_frame.pack_forget()

    def _update_model_progress(self, progress: ModelLoadProgress) -> None:
        self._show_model_progress()
        model_name = progress.model_name
        if progress.stage == "checking":
            self.model_progress_bar.stop()
            self.model_progress_bar.configure(mode="indeterminate", maximum=100)
            self.model_progress_bar.start(12)
            self.model_progress_value.set(f"{model_name} · 正在检查本地模型缓存…")
            self.phase_value.set("正在检查语音识别模型…")
            return
        if progress.stage == "downloading":
            if progress.total > 0:
                completed = min(progress.completed, progress.total)
                percent = completed * 100.0 / progress.total
                self.model_progress_bar.stop()
                self.model_progress_bar.configure(
                    mode="determinate",
                    maximum=progress.total,
                    value=completed,
                )
                self.model_progress_value.set(
                    f"{model_name} · {_format_bytes(completed)} / "
                    f"{_format_bytes(progress.total)} · {percent:.1f}%"
                )
                self.phase_value.set(f"正在下载语音模型 {model_name}…")
            else:
                self.model_progress_bar.stop()
                self.model_progress_bar.configure(mode="indeterminate", maximum=100)
                self.model_progress_bar.start(12)
                self.model_progress_value.set(f"{model_name} · 正在连接下载源…")
                self.phase_value.set(f"正在准备下载语音模型 {model_name}…")
            return
        if progress.stage == "loading":
            self.model_progress_bar.stop()
            self.model_progress_bar.configure(mode="indeterminate", maximum=100)
            self.model_progress_bar.start(12)
            self.model_progress_value.set(f"{model_name} · 下载完成，正在加载模型…")
            self.phase_value.set(f"正在加载语音模型 {model_name}…")
            return
        if progress.stage == "ready":
            self.model_progress_bar.stop()
            self.model_progress_bar.configure(
                mode="determinate", maximum=100, value=100
            )
            self.model_progress_value.set(f"{model_name} · 模型已加载")
            self.phase_value.set("语音模型已加载，正在转写…")
            self.root.after(1200, self._hide_model_progress)
            return
        if progress.stage == "error":
            self.model_progress_bar.stop()
            self.model_progress_value.set(f"{model_name} · 模型下载或加载失败")
            self.phase_value.set("语音模型加载失败，继续导出其他消息…")

    def _append_log(self, message: str) -> None:
        text = message.rstrip()
        tag = ""
        if text.startswith("[成功]"):
            tag = "success"
        elif text.startswith("[错误]") or "失败" in text:
            tag = "error"
        elif "Hook" in text or "密钥" in text or "数据库验证" in text:
            tag = "phase"
        elif text.startswith("数据目录") or text.startswith("发现"):
            tag = "muted"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _is_hook_detail(message: str) -> bool:
        detail_markers = (
            "正在初始化系统调用",
            "正在打开目标进程",
            "目标函数地址",
            "正在分配远程数据缓冲区",
            "正在分配远程伪栈",
            "正在初始化IPC通信",
            "正在准备安装Hook",
            "正在安装远程Hook",
        )
        return any(marker in message for marker in detail_markers)

    def _load_environment(self) -> None:
        if self.working:
            return
        try:
            executable = find_weixin_executable()
            version = verify_supported_version(executable)
            data_root = find_data_root()
            discovered = discover_accounts(data_root)
            newest = max(discovered, key=lambda item: item.data_dir.stat().st_mtime)
            self.accounts.clear()
            labels: list[str] = []
            for account in discovered:
                modified = datetime.fromtimestamp(account.data_dir.stat().st_mtime).strftime("%m-%d %H:%M")
                marker = " · 最近活动" if account is newest else ""
                label = f"{account.wxid}  （{modified}{marker}）"
                labels.append(label)
                self.accounts[label] = account
            previous = self.account_value.get()
            self.account_box["values"] = labels
            self.account_value.set(previous if previous in self.accounts else labels[0])
            self.phase_value.set(f"环境就绪，微信版本 {version}")
            self._append_log(f"数据目录：{data_root}")
            self._append_log(f"发现 {len(labels)} 个账号。请选择要导出的账号。")
        except Exception as exc:
            self.phase_value.set("环境检测失败")
            self._append_log(f"[错误] {exc}")

    def _browse(self) -> None:
        selected = self.filedialog.askdirectory(
            title="选择导出目录", initialdir=self.output_value.get()
        )
        if selected:
            self.output_value.set(selected)

    def _set_working(self, value: bool) -> None:
        self.working = value
        state = "disabled" if value else "normal"
        self.verify_button.configure(state=state)
        self.export_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.account_box.configure(state="disabled" if value else "readonly")
        self.output_entry.configure(state=state)
        self.voice_check.configure(state=state)
        self.voice_model_box.configure(state="disabled" if value else "normal")

    def _start(self, should_export: bool) -> None:
        account = self.accounts.get(self.account_value.get())
        if account is None:
            self.phase_value.set("请先选择目标账号")
            self._append_log("[错误] 请先选择目标账号。")
            return
        output = Path(self.output_value.get()).expanduser()
        self._set_working(True)
        self.cancel_event.clear()
        self.close_requested = False
        self.hook_prompted = False
        self.phase_value.set("正在安装 Hook，等待捕获密钥……")
        self._append_log("—" * 58)
        self._append_log(f"目标账号：{account.wxid}")
        self._append_log("微信应保持运行并停留在未登录界面；看到 Hook 成功后再登录。")
        self._append_log("如果当前已经登录，请等 Hook 成功后切换账号并重新登录。")
        if self.voice_value.get():
            self._append_log(f"语音转文字已开启，模型：{self.voice_model_value.get()}")
        thread = threading.Thread(
            target=self._worker,
            args=(
                account,
                output,
                should_export,
                self.voice_value.get(),
                self.voice_model_value.get(),
            ),
            daemon=True,
        )
        thread.start()

    def _worker(
        self,
        account: Account,
        output: Path,
        should_export: bool,
        transcribe_voice: bool,
        voice_model: str,
    ) -> None:
        writer = _QueueWriter(self.events)
        try:
            data_root = find_data_root()
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                cache_key = account.data_dir.name.casefold()
                key = self.verified_keys.get(cache_key)
                if key is None:
                    key = recover_database_key(account, data_root)
                    print("密钥已捕获，正在验证所选账号数据库……")
                else:
                    print("正在使用本次运行已验证的内存密钥，无需重新登录……")
                with Weixin411Adapter(account, key) as adapter:
                    adapter.validate_key()
                    self.verified_keys[cache_key] = key
                    print("数据库验证成功。")
                    if not should_export:
                        self.events.put(("done", (True, "密钥和数据库验证成功。", None)))
                        return
                    output = output.resolve()
                    output.mkdir(parents=True, exist_ok=True)
                    print("开始导出聊天记录……")
                    result = export_all(
                        adapter,
                        output,
                        transcribe_voice=transcribe_voice,
                        voice_model=voice_model,
                        voice_progress=lambda progress: self.events.put(
                            ("model_progress", progress)
                        ),
                        cancel_event=self.cancel_event,
                    )
                prefix = "导出已停止" if result.cancelled else "导出完成"
                summary = (
                    f"{prefix}：{result.succeeded} 个成功，{result.skipped} 个本地无消息，"
                    f"{result.messages} 条消息，{result.failed} 个真正失败；"
                    f"语音转写 {result.voices_transcribed} 条，失败 {result.voices_failed} 条。"
                )
                self.events.put(("done", (result.failed == 0, summary, result.output_dir)))
        except Exception as exc:
            self.events.put(("done", (False, str(exc), None)))
        finally:
            writer.flush()

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    message = str(payload)
                    if self._is_hook_detail(message):
                        continue
                    if "开始初始化Hook系统" in message:
                        self._append_log("正在安装微信密钥捕获 Hook……")
                        continue
                    self._append_log(message)
                    if "Hook安装成功" in message or "Hook 安装成功" in message:
                        self.phase_value.set("Hook 已安装：请切换账号并重新登录（最长等待 3 分钟）")
                        if not self.hook_prompted:
                            self.hook_prompted = True
                            self._append_log("当前已登录状态不会重复产生密钥事件。")
                            self._append_log("请保持工具运行，在微信中切换账号并重新登录目标账号。")
                elif event == "model_progress":
                    self._update_model_progress(payload)  # type: ignore[arg-type]
                elif event == "done":
                    success, message, output_dir = payload  # type: ignore[misc]
                    self._hide_model_progress()
                    self._set_working(False)
                    self.phase_value.set("操作成功" if success else "操作失败")
                    self._append_log(("[成功] " if success else "[错误] ") + message)
                    if output_dir:
                        self._append_log(f"输出目录：{output_dir}")
                    if self.close_requested:
                        self.root.after_idle(self.root.destroy)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _close(self) -> None:
        if self.working:
            if not self.cancel_event.is_set():
                self.close_requested = True
                self.cancel_event.set()
                self.phase_value.set("正在停止操作并安全清理资源……")
                self._append_log("已请求停止；当前这条语音处理结束后将安全退出，请稍候。")
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if os.name != "nt":
        raise ExporterError("该界面仅支持 Windows。")
    ExporterWindow().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
