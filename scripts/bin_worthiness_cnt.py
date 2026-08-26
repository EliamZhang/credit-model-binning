# -*- coding: utf-8 -*-
"""快捷入口：老客价值模型笔数口径分箱（等价于 scripts/bin_model.py --dataset laoke --model worthiness --metric cnt）。"""
import bin_model

if __name__ == "__main__":
    bin_model.run("laoke", "worthiness", "cnt")
