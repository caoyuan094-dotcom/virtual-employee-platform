#!/usr/bin/env bash
set -u

cd "$(dirname "$0")" || exit 1

echo "虚拟员工平台一键启动"
echo "项目目录：$(pwd)"
echo

./start.sh
status=$?

echo
if [[ $status -eq 0 ]]; then
  echo "服务已停止。"
else
  echo "启动失败或服务异常退出，退出码：$status"
  echo "请查看日志目录：$(pwd)/.logs"
fi
echo
read -r -p "按回车键关闭窗口..."
exit "$status"
