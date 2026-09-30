"""o200k tokenizer from a local file. tiktoken's own download fails on this host
(KOPRI TLS interception + Python 3.14 strict certificate checks), so fetch it once:
    curl -s -o ~/.cache/ocrserver-ab/o200k_base.tiktoken \\
        https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken
"""
import os
import tiktoken.load as L
_P = os.path.expanduser(os.getenv('AB_TOKENIZER', '~/.cache/ocrserver-ab/o200k_base.tiktoken'))
_orig = L.read_file
L.read_file = lambda p: open(_P, 'rb').read() if p.endswith('o200k_base.tiktoken') else _orig(p)
import tiktoken  # noqa: E402

def enc():
    return tiktoken.get_encoding('o200k_base')
