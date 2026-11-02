@echo off
rem Usage: run_client.bat [--server 192.168.0.10:8765]
start "" pythonw "%~dp0todo_client.py" %*
