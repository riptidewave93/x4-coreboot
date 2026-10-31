@echo -off
# Write and verify this build's BIOS region with Intel Fpt (FLASHING.md). Run it from
# the folder that holds Fpt.efi and bios-region.bin.
if not exist Fpt.efi then
  echo "Fpt.efi is missing: copy EFI/Fpt.efi from Radxa's V007 zip next to this script."
  goto END
endif
if not exist bios-region.bin then
  echo "bios-region.bin is missing: copy it from the build next to this script."
  goto END
endif
echo "Writing bios-region.bin. Don't cut power until this script ends."
Fpt.efi -f bios-region.bin -bios
if %lasterror% ne 0 then
  goto FAIL
endif
Fpt.efi -bios -verify bios-region.bin
if %lasterror% ne 0 then
  goto FAIL
endif
echo "Done. Unplug the power, plug it back in and power on."
goto END
:FAIL
echo "Fpt failed. Don't power off: fix the cause and run install.nsh again."
:END
