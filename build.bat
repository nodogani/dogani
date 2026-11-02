@echo off
rem dist\DoganiServer\DoganiServer.exe, dist\DoganiClient\DoganiClient.exe 생성
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install pyinstaller -r server\requirements.txt -r client\requirements.txt || goto :error

python -m PyInstaller --noconfirm --windowed --name DoganiServer --distpath dist --workpath build ^
    server\todo_server.py || goto :error
python -m PyInstaller --noconfirm --windowed --name DoganiClient --distpath dist --workpath build ^
    --add-data "client\ui;ui" client\todo_client.py || goto :error

rem 용량 줄이기: 한국어/영어 외 Qt 번역 파일 삭제
powershell -NoProfile -Command "Get-ChildItem 'dist\DoganiClient\_internal\PySide6\translations' -Recurse -File | Where-Object { $_.Name -notmatch '(_ko|_en|ko\.pak|en-US\.pak)' } | Remove-Item -Force"

echo.
echo 완료: dist\DoganiServer, dist\DoganiClient 폴더를 통째로 복사해서 사용하세요.
pause
exit /b 0

:error
echo 빌드 실패
pause
exit /b 1
