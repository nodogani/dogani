@echo off
chcp 65001 >nul
rem dist\dogani\dogani.exe 생성 (런처). 처음 한 번만 나눠 주면 이후 업데이트는 켤 때 GitHub 태그에서 자동.
rem 다시 빌드가 필요한 때: 새 외부 라이브러리를 쓰게 됐을 때(app\deps.py 에 추가) / launcher.py 를 고쳤을 때
cd /d "%~dp0"
python -m pip install pyinstaller -r app\requirements.txt || goto :error

rem 기본 코드(받은 업데이트가 없을 때 실행)
if exist build\bundle rmdir /s /q build\bundle
robocopy app build\bundle\app /E /XD __pycache__ editor node_modules >nul
if errorlevel 8 goto :error
rem 기본 코드의 버전 = 마지막 태그 (트레이에 표시, 업데이트 비교용). 태그가 없으면 빈 값 = 켤 때 최신 태그를 받음
python -c "import json,subprocess; r=subprocess.run(['git','describe','--tags','--abbrev=0'],capture_output=True,text=True); t=r.stdout.strip() if r.returncode==0 else ''; open('build/bundle/app/version.json','w',encoding='utf-8').write(json.dumps({'id':t,'label':t}))" || goto :error

python -m PyInstaller --noconfirm --windowed --name dogani --distpath dist --workpath build ^
    --add-data "build\bundle\app;app" ^
    app\launcher.py || goto :error

rem 용량 줄이기: 한국어/영어 외 Qt 번역 파일 삭제
powershell -NoProfile -Command "Get-ChildItem 'dist\dogani\_internal\PySide6\translations' -Recurse -File | Where-Object { $_.Name -notmatch '(_ko|_en|ko\.pak|en-US\.pak)' } | Remove-Item -Force"

echo.
echo 완료: dist\dogani 폴더를 통째로 복사해서 사용하세요.
pause
exit /b 0

:error
echo 빌드 실패
pause
exit /b 1
