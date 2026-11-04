@echo off
rem Usage: run.bat [--dir "D:\Obsidian\Daily"] [--port 8765] [--add 192.168.0.10]
start "" pythonw "%~dp0dogani.py" %*
