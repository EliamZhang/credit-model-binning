# -*- coding: utf-8 -*-
"""快捷入口：老客 mlt 金额口径分箱（等价于 scripts/bin_model.py --dataset laoke --model mlt --metric amt）。"""
import bin_model

if __name__ == "__main__":
    bin_model.run("laoke", "mlt", "amt")
