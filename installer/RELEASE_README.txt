WY ZER - WINDOWS INSTALL GUIDE
===============================

What you need
--------------
- A 64-bit Windows 10 (22H2 or newer) or Windows 11 PC
- Internet access for the first installation
- Several GB of free disk space for Wyzer, speech components, and the local AI model

Install Wyzer
-------------
1. Extract this ZIP to a normal folder such as Downloads.
2. Double-click "Install Wyzer.cmd".
   It permits this installer process to run without changing the PC's saved PowerShell policy.
   If Windows asks, allow the installer to run.
3. Keep the setup window open. It automatically installs a private Python runtime, Wyzer,
   Ollama, the local AI model, and speech models. The first setup downloads several GB and can
   take a while.
4. Setup checks everything, creates Desktop and Start Menu shortcuts, and starts Wyzer.

You do not need to open PowerShell or install Python, Ollama, or an AI model yourself.

Using Wyzer
-----------
- Double-click the desktop shortcut, then approve the Windows UAC prompt, to open Wyzer as administrator.
- Double-click the character to open chat.
- Right-click the character for controls, including Quit.
- The first speech start may take a short time while models warm up.

If something goes wrong
-----------------------
1. Quit Wyzer from its tray icon or character menu.
2. Open PowerShell and run:

   & "$env:LOCALAPPDATA\Wyzer\Wyzer Console.cmd"

3. Keep the console open and read or copy the last error message.

The setup log is always available here:

   %LOCALAPPDATA%\Wyzer\install.log

Where Wyzer stores its files
----------------------------
%LOCALAPPDATA%\Wyzer

This folder contains the installed private Python runtime and environment, settings, avatars,
wake-word models, downloaded speech models, memory, and task state. Reinstalling
Wyzer preserves those files.

Important
---------
- An organization-enforced PowerShell, AppLocker, or application-control policy still requires
  approval from the computer administrator.
- OpenWakeWord's required preprocessing models are bundled and verified by the installer; they do
  not require a separate download on the destination PC.
- Wyzer does not change the computer's global Python packages.
- On computers with an NVIDIA GPU, the installer adds CUDA-enabled PyTorch to Wyzer's private environment.
- Do not delete the %LOCALAPPDATA%\Wyzer folder unless you want to remove Wyzer's
  local settings, avatars, downloaded models, memory, and task history.
