@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PY=%~dp0python_embed\python.exe"
set "ZIP=%~dp0python_embed\python311.zip"

echo ========== 嵌入版 Python 快速诊断 ==========
echo.

if not exist "%PY%" (
  echo [错误] 找不到: python_embed\python.exe
  echo 请把整个项目文件夹拷全，或在此电脑运行「准备便携环境.ps1」。
  goto :end
)
echo [OK] python.exe 存在

if not exist "%ZIP%" (
  echo [错误] 缺少 python311.zip（必须与 python.exe 同目录）
  echo 请重新解压官方「Windows embeddable package」到 python_embed，或运行 准备便携环境.ps1 -Recreate
  goto :end
)
for %%A in ("%ZIP%") do echo [信息] python311.zip 大小: %%~zA 字节 ^(正常约 4MB 以上；若为 0 或很小则文件坏/未拷全^)

echo.
echo 尝试加载标准库 encodings...
"%PY%" -c "import encodings; print('[OK] encodings 加载成功')" 2>&1
if errorlevel 1 (
  echo.
  echo [失败] 常见原因：
  echo   1. U 盘/网盘/压缩包只解压了一部分，python311.zip 不完整
  echo   2. 杀毒软件隔离或锁定了 zip
  echo   3. 系统环境变量 PYTHONHOME 指向了别的路径 ^(本脚本已临时清除，若仍失败多为文件问题^)
  echo.
  echo 处理：在本机执行 准备便携环境.ps1 -Recreate，或从 python.org 重新下载同版本 embed 包覆盖 python_embed。
)

:end
echo.
pause
