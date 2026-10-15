#!/usr/bin/env python3
"""修复MD文件中章节标题和条文号粘连的问题
用法: python fix_concat_headers.py <file.md>
示例: # 5.2电梯5.2.1内容 → 拆分为两行
"""
import re
import sys

def fix_concatenated_headers(content):
    """将 '# X.X标题X.X.X内容' 拆分为 '# X.X标题\\nX.X.X内容'
    
    支持的格式：
    1. # 5.2电梯5.2.1内容（标题和条文号粘连）
    2. # 1总则1.0.1内容（章节标题和条文号粘连）
    """
    # 格式1：# X.X标题X.X.X内容（标题和条文号粘连）
    # 加 ()（）: 标题含括号(如"排(蓄)水材料")时字符类要覆盖,否则拆不开(JGJ 155 实测 3 处漏拆)
    pattern1 = r'^(#{1,4}\s+\d{1,2}\.\d{1,2}[\u4e00-\u9fa5a-zA-Z\s、，。；：()（）]+?)(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(.*)$'
    # 格式2：# X标题X.X.X内容（章节标题和条文号粘连，如"1总则1.0.1"）
    pattern2 = r'^(#{1,4}\s+\d{1,2}[\u4e00-\u9fa5a-zA-Z\s、，。；：()（）]+?)(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(.*)$'
    
    lines = content.split('\n')
    fixed = 0
    result = []
    
    for line in lines:
        m = re.match(pattern1, line) or re.match(pattern2, line)
        if m:
            header = m.group(1).rstrip()
            clause_no = m.group(2)
            clause_content = m.group(3)
            result.append(header)
            result.append(f'{clause_no}{clause_content}')
            fixed += 1
        else:
            result.append(line)
    
    return '\n'.join(result), fixed

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python fix_concat_headers.py <file.md>")
        sys.exit(1)
    
    path = sys.argv[1]
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    fixed_content, count = fix_concatenated_headers(content)
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(fixed_content)
    
    print(f'修复了 {count} 处粘连格式')
