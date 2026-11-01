@echo off
rem Converts a KiCad schematic to a Proteus project.
rem Usage: drag a .kicad_sch file onto this file, or run: kicad2proteus.bat C:\path\schematic.kicad_sch
rem Output: output\<name>.pdsprj and output\<name>.rapor.txt (the report); the KiCad files are not changed.
if "%~1"=="" (
  echo Usage: kicad2proteus.bat schematic.kicad_sch
  pause
  exit /b 1
)
if not exist "%~dp0output" mkdir "%~dp0output"
python "%~dp0k2p\k2p.py" "%~f1" "%~dp0output\%~n1.pdsprj"
pause
