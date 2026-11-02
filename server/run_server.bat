@echo off
rem Usage: run_server.bat [--dir "D:\Obsidian\Daily"] [--port 8765]
start "" pythonw "%~dp0todo_server.py" %*
