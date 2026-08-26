# -*- coding: utf-8 -*-
"""快捷入口：老客 mlt × 价值模型全局交叉（等价于 scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode matrix）。"""
import cross_models

if __name__ == "__main__":
    cross_models.run("laoke", "mlt", "worthiness", "matrix")
