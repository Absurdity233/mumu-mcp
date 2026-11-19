#!/bin/bash
# Rebuild the module whose addresses are supplied as data rather than symbols.

export PATH=/f/tools/llvm22/bin:/usr/bin:$PATH
cd /f/kernel/android-6.1.90 || exit 1

cp /f/kernel/khide/khide_hook.c khide_build/khide_hook.c
rm -f khide_build/*.o khide_build/*.ko khide_build/*.mod khide_build/.*.cmd 2>/dev/null

make ARCH=x86_64 \
     HOSTCC=gcc \
     CC="/f/tools/llvm22/bin/clang.exe" \
     LD="/f/tools/llvm22/bin/ld.lld.exe" \
     LLVM= \
     M=khide_build modules 2>&1 \
  | grep -vE 'warning:|note:|^\s*[0-9]+ \||^\s*\||^\s*\^|In file included|from ' \
  | tail -14

echo ""
ls -la khide_build/khide_hook.ko 2>/dev/null || echo "no .ko"
