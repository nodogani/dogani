@echo off
chcp 65001 >nul
rem 편집기 번들 빌드: src\main.js -> ..\ui\vendor\editor.js  (Node.js 필요, 개발 때만)
rem 프로젝트 경로의 특수문자(★) 때문에 esbuild 설치가 깨져서 %TEMP% 에서 빌드한다
setlocal
set "SRC=%~dp0"
set "WORK=%TEMP%\dogani-editor-build"
if not exist "%WORK%" mkdir "%WORK%"
copy /y "%SRC%package.json" "%WORK%\" >nul
if exist "%SRC%package-lock.json" copy /y "%SRC%package-lock.json" "%WORK%\" >nul
robocopy "%SRC%src" "%WORK%\src" /mir /njh /njs /nfl /ndl >nul
pushd "%WORK%"
call npm install --no-audit --no-fund || goto :error
call node_modules\.bin\esbuild src/main.js --bundle --minify --format=iife --target=chrome108 --legal-comments=none --outfile=out/editor.js || goto :error
popd
if not exist "%SRC%..\ui\vendor" mkdir "%SRC%..\ui\vendor"
copy /y "%WORK%\out\editor.js" "%SRC%..\ui\vendor\editor.js" >nul
copy /y "%WORK%\package-lock.json" "%SRC%" >nul
echo 완료: app\ui\vendor\editor.js
exit /b 0
:error
popd
echo 빌드 실패
exit /b 1
