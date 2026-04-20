#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重置预测验证状态"""
import sys
import os

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction

db = SessionLocal()

# 重置所有预测的验证状态
predictions = db.query(Prediction).all()
for p in predictions:
    p.actual_change = None
    p.is_correct = None
    p.verify_count = 0
    p.verify_history = []
    p.current_nav = None
    p.current_nav_date = None
    p.verified_at = None
    p.last_verify_date = None

db.commit()
print(f"已重置 {len(predictions)} 条预测的验证状态")
db.close()
