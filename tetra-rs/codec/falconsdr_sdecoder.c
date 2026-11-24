/* FalconSDR's own stdin/stdout front end for ETSI's TETRA speech decoder (EN 300 395-2, "sdecoder").
 *
 * This file is FalconSDR's (GPL-3.0-or-later). ETSI's reference code is NOT in this repository: the add-on
 * downloads it from etsi.org on the user's computer and links this file against it (falconsdr/addons/tetra_codec.py).
 *
 * Protocol: 138 little-endian 16-bit words in (BFI + 137 bits, one bit per word), 240 samples (8 kHz s16le) out.
 * ETSI's own main() works on files and prints progress to stdout, which would corrupt the PCM stream. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#define DUP _dup
#define DUP2 _dup2
#else
#include <unistd.h>
#define DUP dup
#define DUP2 dup2
#endif
#include "source.h"

#define L_FRAME 240
#define SERIAL_SIZE 138
#define PRM_SIZE 24

int main(void)
{
    Word16 serial[SERIAL_SIZE], parm[PRM_SIZE], synth[L_FRAME];
    /* Keep the real stdout for PCM; whatever the library printf()s goes to stderr. */
    int out_fd = DUP(1);
    FILE *out;
    if (out_fd < 0) return 1;
    DUP2(2, 1);
#ifdef _WIN32
    _setmode(0, _O_BINARY);
    _setmode(out_fd, _O_BINARY);
    out = _fdopen(out_fd, "wb");
#else
    out = fdopen(out_fd, "wb");
#endif
    if (out == NULL) return 1;
    Init_Decod_Tetra();
    while (fread(serial, sizeof(Word16), SERIAL_SIZE, stdin) == SERIAL_SIZE) {
        Bits2prm_Tetra(serial, parm);
        Decod_Tetra(parm, synth);
        Post_Process(synth, (Word16)L_FRAME);
        if (fwrite(synth, sizeof(Word16), L_FRAME, out) != L_FRAME || fflush(out) != 0) return 1;
    }
    return 0;
}
