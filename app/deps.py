"""exe(런처) 빌드용 의존성 목록. 받아 오는 코드가 쓸 수 있는 모듈은 여기 import 해 두어야 exe 에 들어간다.
새 외부 라이브러리를 쓰게 되면 여기에 추가하고 exe 를 한 번 다시 빌드한다 (그때만 빌드 필요)."""
# 표준 라이브러리 (PyInstaller 는 import 된 것만 넣으므로 넉넉하게)
import argparse, base64, collections, contextlib, ctypes, ctypes.wintypes, dataclasses, datetime, functools  # noqa
import hashlib, hmac, http.client, http.server, io, itertools, json, logging, pathlib, queue, random, re  # noqa
import secrets, shutil, socket, socketserver, string, subprocess, tempfile, threading, time, traceback  # noqa
import urllib.parse, urllib.request, uuid, winreg, zipfile  # noqa

# 외부 라이브러리
import pystray, pystray._win32  # noqa
from PIL import Image, ImageDraw, ImageFont  # noqa
from PySide6 import QtCore, QtGui, QtNetwork, QtWebChannel, QtWebEngineCore, QtWebEngineWidgets, QtWidgets  # noqa
