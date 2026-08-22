@echo off
rem Silence FFmpeg/codec chatter. Must be set before Python starts: a DLL
rem loaded later does not pick up in-process os.environ changes on Windows.
set OPENCV_LOG_LEVEL=SILENT
set OPENCV_FFMPEG_LOGLEVEL=-8
start "" pythonw "%~dp0capture.py"
