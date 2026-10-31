@echo -off
# Back up the whole flash with Intel Fpt before the first install (FLASHING.md).
if not exist Fpt.efi then
  echo "Fpt.efi is missing: copy EFI/Fpt.efi from Radxa's V007 zip next to this script."
  goto END
endif
Fpt.efi -d backup1.bin
if %lasterror% ne 0 then
  goto FAIL
endif
Fpt.efi -d backup2.bin
if %lasterror% ne 0 then
  goto FAIL
endif
echo "Done. Copy backup1.bin and backup2.bin off the stick; their sha256 must match."
goto END
:FAIL
echo "Fpt failed: no backup was made."
:END
