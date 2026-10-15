"""配套工具：入库前后质检、MD 修复、向量定点更新、转换后处理。

这些是「按需运行的运维脚本」，不属于日常查询路径：
  python -m stdq.tools.verify_md_vs_db      # 入库对账
  python -m stdq.tools.stdq_check           # 条文号连续性
  ...（每个模块 --help 看用法）
"""
