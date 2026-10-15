#!/usr/bin/env python3
"""MinerU 转换后的 MD 后处理脚本

修复问题：
1. 二级标题误判：加粗条文被 MinerU 用 ## 包裹，需改回普通文本
2. 图片路径：确保使用绝对路径

用法：python3 mineru_postprocess.py <md文件或目录> [图片目录]
"""

import re
import sys
import os


def fix_headers(md_text: str) -> str:
    """修复 MinerU 误判的二级标题。
    
    规则（满足任一即判定为条文内容，去掉 ##）：
    - 包含句号/冒号/分号 → 完整句子
    - 三位编号（X.X.X）后无空格直接中文 → 条文（有空格是术语定义）
    - 以 (1) (2) 编号开头
    """
    lines = md_text.split('\n')
    result = []
    for line in lines:
        if line.startswith('## '):
            content = line[3:].strip()
            is_false_header = False
            
            if '。' in content or '：' in content or '；' in content:
                is_false_header = True
            elif re.match(r'^\d+\.\d+\.\d+[^\s\d]', content) and \
                 re.match(r'^\d+\.\d+\.\d+[^\s\d][\u4e00-\u9fff]', content):
                is_false_header = True
            elif re.match(r'^\(\d+\)', content):
                is_false_header = True
            
            if is_false_header:
                result.append(content)
            else:
                result.append(line)
        else:
            result.append(line)
    return '\n'.join(result)


def fix_image_paths(md_text: str, img_dir: str) -> str:
    """确保图片引用使用绝对路径。"""
    md_text = re.sub(
        r'\!\[([^\]]*)\]\(images/',
        f'![\\1]({img_dir}/',
        md_text
    )
    md_text = re.sub(
        r'src="images/',
        f'src="{img_dir}/',
        md_text
    )
    return md_text


def process_file(md_path: str, img_dir: str = None):
    """处理单个 MD 文件。"""
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    content = fix_headers(content)
    
    if img_dir:
        content = fix_image_paths(content, img_dir)
    
    orig_lines = original.split('\n')
    new_lines = content.split('\n')
    fixed_headers = sum(1 for o, n in zip(orig_lines, new_lines) 
                        if o.startswith('## ') and not n.startswith('## '))
    
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"  {os.path.basename(md_path)}: 修复 {fixed_headers} 个误判标题")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: mineru_postprocess.py <md文件或目录> [图片目录]")
        sys.exit(1)
    
    target = sys.argv[1]
    img_dir = sys.argv[2] if len(sys.argv) > 2 else None
    
    if os.path.isdir(target):
        for f in sorted(os.listdir(target)):
            if f.endswith('.md'):
                md_path = os.path.join(target, f)
                if img_dir is None:
                    base = os.path.splitext(f)[0]
                    candidate = os.path.join(target, 'images', base)
                    if os.path.isdir(candidate):
                        img_dir_arg = candidate
                    else:
                        img_dir_arg = None
                else:
                    img_dir_arg = img_dir
                process_file(md_path, img_dir_arg)
    else:
        process_file(target, img_dir)
