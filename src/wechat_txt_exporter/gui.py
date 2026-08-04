from __future__ import annotations

import contextlib
import ctypes
import calendar
import os
import queue
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .adapter import Weixin411Adapter
from .discovery import (
    discover_accounts,
    find_data_root,
    find_weixin_executable,
    verify_supported_version,
)
from .errors import ExporterError
from .exporter import export_all, parse_since_date
from .key_recovery import recover_database_key
from .models import Account
from .voice import (
    LOCAL_WHISPER_LARGE_MODEL,
    LOCAL_WHISPER_MODEL,
    SILICONFLOW_MODEL,
    validate_siliconflow_api_key,
)


LOCAL_WHISPER_LABEL = "Whisper small（本地）"
LOCAL_WHISPER_LARGE_LABEL = "Whisper large-v3（本地）"
SENSEVOICE_LABEL = "SenseVoiceSmall（SiliconFlow API）"


def _voice_model_id(value: str) -> str:
    if value == LOCAL_WHISPER_LARGE_LABEL:
        return LOCAL_WHISPER_LARGE_MODEL
    if value == SENSEVOICE_LABEL:
        return SILICONFLOW_MODEL
    return LOCAL_WHISPER_MODEL


def _voice_model_label(value: str) -> str:
    if value == LOCAL_WHISPER_LARGE_MODEL:
        return LOCAL_WHISPER_LARGE_LABEL
    if value == SILICONFLOW_MODEL:
        return SENSEVOICE_LABEL
    return LOCAL_WHISPER_LABEL


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
        self.force_close_allowed = False
        self.cancel_event = threading.Event()
        self.close_requested = False

        self.account_value = tk.StringVar()
        self.output_value = tk.StringVar(
            value=str(Path(__file__).resolve().parents[2] / "exports")
        )
        self.voice_value = tk.BooleanVar(value=True)
        self.since_date_value = tk.StringVar()
        configured_voice_model = os.environ.get("WECHAT_VOICE_MODEL", LOCAL_WHISPER_MODEL)
        self.preferred_voice_model = (
            configured_voice_model
            if configured_voice_model
            in {LOCAL_WHISPER_MODEL, LOCAL_WHISPER_LARGE_MODEL, SILICONFLOW_MODEL}
            else LOCAL_WHISPER_MODEL
        )
        self.voice_model_value = tk.StringVar(
            value=_voice_model_label(self.preferred_voice_model)
        )
        self.phase_value = tk.StringVar(value="正在检测微信环境……")
        self.voice_progress_value = tk.DoubleVar(value=0)
        self.voice_progress_text = tk.StringVar(value="尚未开始")
        self.voice_wait_started: float | None = None
        self.voice_wait_base = ""
        self.date_picker_window = None

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(10, self._start_voice_api_check)
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
        ttk.Label(form, text="起始日期", width=10).grid(row=2, column=0, sticky="w", pady=5)
        date_options = ttk.Frame(form)
        date_options.grid(row=2, column=1, columnspan=2, sticky="w", pady=5)
        self.since_date_entry = ttk.Entry(
            date_options,
            textvariable=self.since_date_value,
            width=14,
            state="readonly",
        )
        self.since_date_entry.pack(side="left")
        self.date_select_button = ttk.Button(
            date_options,
            text="选择日期…",
            command=self._open_date_picker,
        )
        self.date_select_button.pack(side="left", padx=(8, 5))
        self.date_clear_button = ttk.Button(
            date_options,
            text="清除",
            command=lambda: self.since_date_value.set(""),
        )
        self.date_clear_button.pack(side="left")
        ttk.Label(date_options, text="留空导出全部").pack(side="left", padx=(8, 0))
        ttk.Label(form, text="语音消息", width=10).grid(row=3, column=0, sticky="w", pady=5)
        voice_options = ttk.Frame(form)
        voice_options.grid(row=3, column=1, columnspan=2, sticky="w", pady=5)
        self.voice_check = ttk.Checkbutton(
            voice_options, text="转成文字并写入 TXT", variable=self.voice_value
        )
        self.voice_check.pack(side="left")
        ttk.Label(voice_options, text="识别模型：").pack(side="left", padx=(18, 5))
        self.voice_model_box = ttk.Combobox(
            voice_options,
            textvariable=self.voice_model_value,
            values=(
                LOCAL_WHISPER_LABEL,
                LOCAL_WHISPER_LARGE_LABEL,
            ),
            width=30,
            state="readonly",
        )
        self.voice_model_box.pack(side="left")
        form.columnconfigure(1, weight=1)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(14, 10))
        self.verify_button = ttk.Button(
            actions,
            text="验证数据库访问",
            command=lambda: self._start(False),
        )
        self.verify_button.pack(side="left")
        self.export_button = ttk.Button(
            actions, text="一键更新全部 TXT", command=lambda: self._start(True)
        )
        self.export_button.pack(side="left", padx=10)
        self.rebuild_button = ttk.Button(
            actions,
            text="强制全量重建",
            command=lambda: self._start(True, force_full=True),
        )
        self.rebuild_button.pack(side="left")

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(2, 6))
        ttk.Label(status, text="当前状态：").pack(side="left")
        ttk.Label(status, textvariable=self.phase_value, foreground="#1261a0").pack(side="left")

        voice_status = ttk.Frame(outer)
        voice_status.pack(fill="x", pady=(0, 8))
        ttk.Label(voice_status, text="语音进度：").pack(side="left")
        self.voice_progress_bar = ttk.Progressbar(
            voice_status,
            variable=self.voice_progress_value,
            maximum=100,
            length=240,
        )
        self.voice_progress_bar.pack(side="left", padx=(0, 8))
        ttk.Label(voice_status, textvariable=self.voice_progress_text).pack(side="left")

        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_frame,
            height=17,
            state="disabled",
            wrap="word",
            font=("Consolas", 10),
            background="#fafafa",
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("success", foreground="#16713b")
        self.log.tag_configure("error", foreground="#b42318")
        self.log.tag_configure("phase", foreground="#1261a0")
        self.log.tag_configure("muted", foreground="#666666")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _append_log(self, message: str) -> None:
        text = message.rstrip()
        tag = ""
        if text.startswith("[成功]"):
            tag = "success"
        elif text.startswith("[错误]") or "失败" in text:
            tag = "error"
        elif "密钥" in text or "数据库验证" in text:
            tag = "phase"
        elif text.startswith("数据目录") or text.startswith("发现"):
            tag = "muted"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start_voice_api_check(self) -> None:
        threading.Thread(target=self._check_voice_api, daemon=True).start()

    def _check_voice_api(self) -> None:
        valid, message = validate_siliconflow_api_key()
        self.events.put(("voice_api", (valid, message)))

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

    def _open_date_picker(self) -> None:
        if self.date_picker_window is not None:
            try:
                self.date_picker_window.lift()
                self.date_picker_window.focus_force()
                return
            except Exception:
                self.date_picker_window = None

        try:
            selected = datetime.strptime(
                self.since_date_value.get(), "%Y-%m-%d"
            ).date()
        except ValueError:
            selected = date.today()

        tk, ttk = self.tk, self.ttk
        picker = tk.Toplevel(self.root)
        self.date_picker_window = picker
        picker.title("选择导出起始日期")
        picker.resizable(False, False)
        picker.transient(self.root)
        picker.geometry(
            f"+{self.since_date_entry.winfo_rootx()}"
            f"+{self.since_date_entry.winfo_rooty() + self.since_date_entry.winfo_height()}"
        )
        current = [selected.year, selected.month]
        body = ttk.Frame(picker, padding=10)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        title = ttk.Label(header, anchor="center")
        days = ttk.Frame(body)
        days.pack(pady=(8, 4))

        def close() -> None:
            self.date_picker_window = None
            picker.destroy()

        def choose(day: int) -> None:
            self.since_date_value.set(
                f"{current[0]:04d}-{current[1]:02d}-{day:02d}"
            )
            close()

        def draw() -> None:
            title.configure(text=f"{current[0]} 年 {current[1]} 月")
            for child in days.winfo_children():
                child.destroy()
            for column, label in enumerate(("一", "二", "三", "四", "五", "六", "日")):
                ttk.Label(days, text=label, width=4, anchor="center").grid(
                    row=0, column=column
                )
            for row, week in enumerate(
                calendar.monthcalendar(current[0], current[1]), start=1
            ):
                for column, day in enumerate(week):
                    if day:
                        ttk.Button(
                            days,
                            text=str(day),
                            width=3,
                            command=lambda value=day: choose(value),
                        ).grid(row=row, column=column, padx=1, pady=1)

        def move_month(offset: int) -> None:
            month = current[1] + offset
            current[0] += (month - 1) // 12
            current[1] = (month - 1) % 12 + 1
            draw()

        ttk.Button(header, text="‹", width=3, command=lambda: move_month(-1)).grid(
            row=0, column=0
        )
        title.grid(row=0, column=1, sticky="ew", padx=12)
        ttk.Button(header, text="›", width=3, command=lambda: move_month(1)).grid(
            row=0, column=2
        )
        header.columnconfigure(1, weight=1)
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(4, 0))
        ttk.Button(
            footer,
            text="今天",
            command=lambda: (
                self.since_date_value.set(date.today().strftime("%Y-%m-%d")),
                close(),
            ),
        ).pack(side="left")
        ttk.Button(footer, text="取消", command=close).pack(side="right")
        picker.protocol("WM_DELETE_WINDOW", close)
        draw()
        picker.grab_set()

    def _set_working(self, value: bool) -> None:
        self.working = value
        state = "disabled" if value else "normal"
        self.verify_button.configure(state=state)
        self.export_button.configure(state=state)
        self.rebuild_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.account_box.configure(state="disabled" if value else "readonly")
        self.output_entry.configure(state=state)
        self.since_date_entry.configure(state="disabled" if value else "readonly")
        self.date_select_button.configure(state=state)
        self.date_clear_button.configure(state=state)
        self.voice_check.configure(state=state)
        self.voice_model_box.configure(state="disabled" if value else "readonly")

    def _start(self, should_export: bool, *, force_full: bool = False) -> None:
        account = self.accounts.get(self.account_value.get())
        if account is None:
            self.phase_value.set("请先选择目标账号")
            self._append_log("[错误] 请先选择目标账号。")
            return
        output = Path(self.output_value.get()).expanduser()
        try:
            since = parse_since_date(self.since_date_value.get())
        except ValueError as exc:
            self.phase_value.set("起始日期格式错误")
            self._append_log(f"[错误] {exc}")
            return
        since_date = since[0] if since is not None else ""
        self._set_working(True)
        self.force_close_allowed = False
        self.cancel_event.clear()
        self.close_requested = False
        self.voice_progress_value.set(0)
        self.voice_progress_text.set("等待语音处理")
        self.voice_wait_started = None
        self.voice_wait_base = ""
        voice_model_label = self.voice_model_value.get()
        voice_model = _voice_model_id(voice_model_label)
        self.phase_value.set("正在准备登录密钥捕获……")
        self._append_log("—" * 58)
        self._append_log(f"目标账号：{account.wxid}")
        self._append_log(
            f"导出范围：{since_date} 00:00:00 起" if since_date else "导出范围：全部消息"
        )
        self._append_log("请先让微信停留在登录界面；看到“密钥捕获已就绪”后登录目标账号。")
        if self.voice_value.get():
            self._append_log(f"语音转文字已开启，模型：{voice_model_label}")
        if force_full:
            self._append_log("已选择强制全量重建，将忽略会话变化状态。")
        thread = threading.Thread(
            target=self._worker,
            args=(
                account,
                output,
                should_export,
                self.voice_value.get(),
                voice_model,
                force_full,
                since_date,
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
        force_full: bool,
        since_date: str,
    ) -> None:
        writer = _QueueWriter(self.events)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                cache_key = account.data_dir.name.casefold()
                key = self.verified_keys.get(cache_key)
                if key is None:
                    key = recover_database_key(account)
                    self.events.put(("phase", "正在验证所选账号数据库……"))
                    print("密钥已获取，正在验证所选账号数据库……")
                else:
                    self.events.put(("phase", "正在验证所选账号数据库……"))
                    print("正在使用本次运行已验证的内存密钥……")
                with Weixin411Adapter(account, key) as adapter:
                    adapter.validate_key()
                    self.verified_keys[cache_key] = key
                    print("数据库验证成功。")
                    if not should_export:
                        self.events.put(
                            (
                                "done",
                                (True, "数据库访问验证成功，密钥已在本次运行中缓存。", None),
                            )
                        )
                        return
                    self.force_close_allowed = True
                    self.events.put(("phase", "正在准备导出聊天记录……"))
                    output = output.resolve()
                    output.mkdir(parents=True, exist_ok=True)
                    print("开始导出聊天记录……")
                    result = export_all(
                        adapter,
                        output,
                        transcribe_voice=transcribe_voice,
                        voice_model=voice_model,
                        cancel_event=self.cancel_event,
                        progress=lambda message: self.events.put(("phase", message)),
                        voice_progress=lambda current, total, status, conversation: self.events.put(
                            (
                                "voice_progress",
                                (current, total, status, conversation),
                            )
                        ),
                        force_full=force_full,
                        since_date=since_date,
                    )
                prefix = "导出已停止" if result.cancelled else "导出完成"
                summary = (
                    f"{prefix}：{result.succeeded} 个已更新，{result.unchanged} 个未变化，"
                    f"{result.skipped} 个本地无消息，"
                    f"{result.messages} 条消息，{result.failed} 个真正失败；"
                    f"语音新转写 {result.voices_transcribed} 条，复用 {result.voices_cached} 条，"
                    f"失败 {result.voices_failed} 条。"
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
                    self._append_log(message)
                    if "密钥捕获已就绪" in message:
                        self.phase_value.set("捕获已就绪：请登录目标微信账号")
                elif event == "voice_api":
                    valid, message = payload  # type: ignore[misc]
                    values = (
                        (
                            LOCAL_WHISPER_LABEL,
                            LOCAL_WHISPER_LARGE_LABEL,
                            SENSEVOICE_LABEL,
                        )
                        if valid
                        else (LOCAL_WHISPER_LABEL, LOCAL_WHISPER_LARGE_LABEL)
                    )
                    self.voice_model_box["values"] = values
                    preferred_label = _voice_model_label(self.preferred_voice_model)
                    if preferred_label in values:
                        self.voice_model_value.set(preferred_label)
                    elif self.voice_model_value.get() not in values:
                        self.voice_model_value.set(LOCAL_WHISPER_LABEL)
                    prefix = "[成功] " if valid else "[提示] "
                    self._append_log(prefix + str(message))
                elif event == "phase":
                    self.phase_value.set(str(payload))
                elif event == "voice_progress":
                    current, total, status, conversation = payload  # type: ignore[misc]
                    current = int(current)
                    total = max(1, int(total))
                    status = str(status)
                    conversation = str(conversation)
                    completed = current - 1 if status.startswith("正在识别") else current
                    self.voice_progress_value.set(completed * 100 / total)
                    self.voice_wait_base = (
                        f"{current}/{total} · {conversation} · {status}"
                    )
                    self.voice_progress_text.set(self.voice_wait_base)
                    self.voice_wait_started = (
                        time.monotonic() if status.startswith("正在识别") else None
                    )
                elif event == "done":
                    success, message, output_dir = payload  # type: ignore[misc]
                    self.force_close_allowed = False
                    self.voice_wait_started = None
                    self._set_working(False)
                    self.phase_value.set("操作成功" if success else "操作失败")
                    self._append_log(("[成功] " if success else "[错误] ") + message)
                    if output_dir:
                        self._append_log(f"输出目录：{output_dir}")
                    if self.close_requested:
                        self.root.after_idle(self.root.destroy)
        except queue.Empty:
            pass
        if self.voice_wait_started is not None:
            elapsed = max(0, int(time.monotonic() - self.voice_wait_started))
            self.voice_progress_text.set(
                f"{self.voice_wait_base} · 已等待 {elapsed} 秒"
            )
        self.root.after(100, self._drain_events)

    def _close(self) -> None:
        if self.working:
            self.cancel_event.set()
            if self.force_close_allowed:
                self.root.destroy()
                return
            if not self.close_requested:
                self.close_requested = True
                self.phase_value.set("正在停止操作并安全清理资源……")
                self._append_log("正在安全移除微信进程断点，完成后将自动退出……")
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
