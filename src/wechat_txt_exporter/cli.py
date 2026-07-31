from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .adapter import Weixin411Adapter
from .discovery import (
    discover_accounts,
    find_data_root,
    find_weixin_executable,
    select_account,
    verify_supported_version,
)
from .errors import ExporterError
from .exporter import export_all
from .key_recovery import recover_database_key
from .voice import LOCAL_WHISPER_LARGE_MODEL, LOCAL_WHISPER_MODEL, SILICONFLOW_MODEL


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-txt-exporter",
        description="将 Windows 微信 4.1.x 的好友和群聊记录分别导出为 TXT。",
    )
    parser.add_argument("--account", help="指定 wxid，跳过交互式账号选择")
    parser.add_argument(
        "--output",
        type=Path,
        default=_project_root() / "exports",
        help="输出根目录（默认：程序目录下的 exports）",
    )
    parser.add_argument("--verbose", action="store_true", help="显示结构诊断信息")
    parser.add_argument(
        "--voice-transcribe", action="store_true", help="将 Silk 语音转成文字并写入 TXT"
    )
    parser.add_argument(
        "--voice-model",
        choices=(LOCAL_WHISPER_MODEL, LOCAL_WHISPER_LARGE_MODEL, SILICONFLOW_MODEL),
        default=LOCAL_WHISPER_MODEL,
        help="语音模型：本地 Whisper small/large-v3 或 SiliconFlow SenseVoiceSmall",
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="忽略增量状态，强制重建全部 TXT",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def run(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise ExporterError("该工具仅支持 Windows。")
    executable = find_weixin_executable()
    version = verify_supported_version(executable)
    data_root = find_data_root()
    accounts = discover_accounts(data_root)
    account = select_account(accounts, args.account)

    print(f"微信版本：{version}")
    print(f"数据目录：{data_root}")
    print(f"目标账号：{account.wxid}")
    print("正在获取本地数据库密钥……")
    key = recover_database_key(account)

    output_root = args.output.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    with Weixin411Adapter(account, key, verbose=args.verbose) as adapter:
        adapter.validate_key()
        print("数据库验证成功，开始导出……")
        result = export_all(
            adapter,
            output_root,
            transcribe_voice=args.voice_transcribe,
            voice_model=args.voice_model,
            force_full=args.force_full,
        )

    print()
    print(f"输出目录：{result.output_dir}")
    print(f"更新会话：{result.succeeded}")
    print(f"未变化会话：{result.unchanged}")
    print(f"本地无消息：{result.skipped}")
    print(f"消息总数：{result.messages}")
    if args.voice_transcribe:
        print(
            f"语音转写：{result.voices_transcribed} 条新增，"
            f"{result.voices_cached} 条复用，{result.voices_failed} 条失败"
        )
    print(f"失败会话：{result.failed}")
    if result.failures:
        print("失败详情：")
        for username, reason in result.failures:
            print(f"  - {username}: {reason}")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 1
    except ExporterError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        if args.verbose:
            raise
        return 1
    except Exception as exc:
        print(f"[错误] 未预期的失败：{exc}", file=sys.stderr)
        if args.verbose:
            raise
        return 1
