# shellcheck shell=bash
# Tab completion of thinny in bash, installed by install.sh: thinny itself says what fits the word being typed.
_thinny() {
	mapfile -t COMPREPLY < <(/usr/local/sbin/thinny complete "${COMP_WORDS[@]:1:COMP_CWORD}" 2>/dev/null)
}
complete -F _thinny thinny
