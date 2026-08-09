WY ZER - WINDOWS INSTALL GUIDE
===============================

What you need
--------------
- A Windows PC
- Internet access for the first installation (speech packages, speech models, and the AI model download once)
- Ollama and the `qwen3.5:4b` model. Wyzer uses this local AI model for chat and actions.

Install Python, Ollama, and Wyzer's AI model
---------------------------------------------
Open PowerShell and run these commands one at a time:

   winget install -e --id Python.Python.3.11
   winget install -e --id Ollama.Ollama

Close PowerShell, open a new PowerShell window, then download Wyzer's model:

   ollama pull qwen3.5:4b

The model download is about 3.4 GB. You can check that it is ready with:

   ollama list

If `ollama` is not recognized after installation, restart Windows once and try again.

Install Wyzer
-------------
1. Complete the Python, Ollama, and model setup above.
2. Extract this ZIP to a normal folder such as Downloads.
3. Right-click install.ps1 and choose "Run with PowerShell".
   If Windows asks, allow the script to run.
4. Wait for the install to finish. The first install can take several minutes.
5. Use the new "Wyzer" desktop shortcut.

Using Wyzer
-----------
- Double-click the desktop shortcut to open the character.
- Double-click the character to open chat.
- Right-click the character for controls, including Quit.
- The first speech start may take a short time while models warm up.

If something goes wrong
-----------------------
1. Quit Wyzer from its tray icon or character menu.
2. Open PowerShell and run:

   & "$env:LOCALAPPDATA\Wyzer\Wyzer Console.cmd"

3. Keep the console open and read or copy the last error message.

Where Wyzer stores its files
----------------------------
%LOCALAPPDATA%\Wyzer

This folder contains the installed private Python environment, settings, avatars,
wake-word models, downloaded speech models, memory, and task state. Reinstalling
Wyzer preserves those files.

Important
---------
- Wyzer does not change the computer's global Python packages.
- Do not delete the %LOCALAPPDATA%\Wyzer folder unless you want to remove Wyzer's
  local settings, avatars, downloaded models, memory, and task history.
