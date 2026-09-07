Set WshShell = CreateObject("WScript.Shell")

' Start Flask server in background
WshShell.Run "cmd /c python app.py", 0, False

' Wait 3 seconds for server to start
WScript.Sleep 3000

' Open browser to Flask URL (shows splash page)
WshShell.Run "http://127.0.0.1:5000"