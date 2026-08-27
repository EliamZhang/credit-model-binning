# -*- coding: utf-8 -*-
"""PreToolUse hook：拦截 git push 命令，强制要求用户确认（项目约定：推送前必须经用户确认）。

输入：stdin 上的 PreToolUse JSON（含 tool_name / tool_input.command）。
输出：仅当命令是 git push（含 -u / --set-upstream / origin 等变体）时，
输出 permissionDecision=ask 的 JSON，让 Claude Code 弹出确认框；
其余命令不输出任何内容（放行，不干预）。
"""
import json
import re
import sys

PUSH_PATTERN = re.compile(r"(^|[;&|]\s*)git\s+push\b")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not PUSH_PATTERN.search(command):
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "项目约定：推送（git push）前必须经用户确认",
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
