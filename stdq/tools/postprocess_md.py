#!/usr/bin/env python3
"""
RapidDoc MD 后处理脚本
用法: python postprocess_md.py input.md [output.md]
不指定 output 则覆盖原文件
"""
import re
import sys
import os


def fix_formula_spacing(text):
    """修复 LaTeX 公式中数字间距偏大 + 格式清理"""
    def fix_one(m):
        formula = m.group(0)
        # 合并分散数字: 2 5 0 0 → 2500
        changed = True
        while changed:
            new = re.sub(r'(\d)\s+(\d)', r'\1\2', formula)
            changed = (new != formula)
            formula = new
        # \mathrm { X } → \mathrm{X}
        formula = re.sub(r'\\mathrm\s*\{\s*(\w+)\s*\}', r'\\mathrm{\1}', formula)
        # ^ { N } → ^{N}
        formula = re.sub(r'\^\s*\{\s*(\d+)\s*\}', r'^{\1}', formula)
        # _ { N } → _{N}
        formula = re.sub(r'_\s*\{\s*(\w+)\s*\}', r'_{\1}', formula)
        # 清理公式两端多余空格
        formula = re.sub(r'\$\s+', '$', formula)
        formula = re.sub(r'\s+\$', '$', formula)
        return formula
    return re.sub(r'\$[^$]+\$', fix_one, text)


def cleanup_whitespace(text):
    """清理多余空行（连续3行以上空行压缩为2行）"""
    return re.sub(r'\n{3,}', '\n\n', text)


def remove_header_footer_noise(text):
    """清理页眉页脚残留噪声（如标准编号重复出现的孤立行）"""
    # 保留第一个出现的标准编号，删除后续重复的孤立行
    lines = text.split('\n')
    seen_std_header = False
    result = []
    for line in lines:
        stripped = line.strip()
        # 检测是否为标准编号孤立行（如 "GB XXXXX—XXXX"）
        if re.match(r'^GB\s+\d+—\d{4}$', stripped):
            if seen_std_header:
                continue  # 跳过重复的标准编号
            seen_std_header = True
        result.append(line)
    return '\n'.join(result)


def fix_clause_number_formats(text):
    """修复 OCR 造成的条文号格式错误(4 类,实测发现)

    R1: 条文号被 4 位年份吃掉 —— `10.0.101991年` → `10.0.10 1991年`
        根因: OCR 把条文号后紧跟的年份粘连,rebuild 吞掉条文号(实测
        (实测有条文因此丢失)
    R2: 公式后紧接条文号 —— `$$...$$5.1.11组合幕墙` → 拆行
        根因: LaTeX 公式 `$$...$$` 后直接跟下一条文号,下一条文被
        rebuild 吞进公式所在条文的 content(实测有条文因此丢失)
    R3: 条文号数字间空格 —— `2. 1. 23` → `2.1.23`;目录行 `5. 2 承载力` → `5.2 承载力`
        根因: OCR 在数字间插入空格,条文号识别失败(实测有这类残留)
    R4: 条文号"只有部分点号带空格" —— `2.0. 33` → `2.0.33`
        根因: 同 R3,但 R3a 要求每段都带空格,只带一段的漏网(实测《建筑照明设计标准》
        某规范的术语行 `## 2.0. 33`、另一规范
        编号被错解析成更短的编号并生成垃圾条目)
        同时修 R3a 只认 3 段的缺陷 —— 实测 `7. 4. 1. 1` 被 R3a 改成 `7.4.1. 1` **留半截**;
        R4 支持 2~5 段(GB/T 7106-2019 / GB/T 15227-2019 用 5 段编号)
    R5: 条文号后多一个句点 —— `3.3.7.保水率试验` → `3.3.7 保水率试验`
        根因: OCR 把编号后的空格抄成句点(实测多处，        如 `## 4.10.`),导致正文 content 以 `.` 开头
    """
    # R1: X.Y.Z + 4位年份(19xx/20xx)粘连 → 拆开(只修行首,避免误伤表格内数字数据)
    #     实测：表格内的长数字串会被全局规则误匹配,必须 (?m)^\s* 锚定行首
    text = re.sub(r'(?m)^\s*(\d{1,2}\.\d{1,2}\.\d{1,2})((?:19|20)\d{2})', r'\1 \2', text)
    # R2: $$...$$ 后直接跟 X.Y.Z → 拆行(避免下一条文被吞)
    text = re.sub(r'(\$\$[^$\n]*?\$\$)(\d{1,2}\.\d{1,2}\.\d{1,2})', r'\1\n\n\2', text)
    # R3a: 三段条文号 X. Y. Z → X.Y.Z
    text = re.sub(r'(\d+)\.\s+(\d+)\.\s+(\d+)', r'\1.\2.\3', text)
    # R3b: 目录行/标题 X. Y 后跟中文 → X.Y(行首,避免误改正文)
    # lookahead 用 [ \t]* 允许 Y 后跟空格再中文(如 "5. 2 承载力"),不跨行
    text = re.sub(r'(?m)(^\s*(?:#{1,6}\s+)?)(\d{1,2})\.\s+(\d{1,2})(?=[ \t]*[\u4e00-\u9fff])', r'\1\2.\3', text)
    # R4: 行首条文号里的"部分点号带空格" → 全部去掉(2~5 段)
    #     行首锚定 + 必须原文含空格才触发 → 正常写法零影响
    text = re.sub(
        r'(?m)^(\s*(?:#{1,6}\s+)?)(\d{1,2}(?:\.\s*\d{1,2}){1,4})(?=[\s\u4e00-\u9fff])',
        lambda m: m.group(1) + re.sub(r'\s+', '', m.group(2)), text)
    # R5: 编号后多余的句点 → 去掉换成单个空格(行首,编号后紧跟正文)
    #     `\.[ \t]*` 连同句点后的空格一起吃掉,避免出现双空格;lookahead 限 CJK
    text = re.sub(r'(?m)^(\s*(?:#{1,6}\s+)?\d{1,2}(?:\.\d{1,2}){1,4})\.[ \t]*(?=[\u4e00-\u9fff])', r'\1 ', text)
    # R6: 编号里的点被 OCR 成冒号—— `6.6:6` → `6.6.6`
    #     实测有真条文号笔误的情形（正文整条在库外,
    #     被误判成"整本找不到编号"的丢条）。
    #     行首锚定 + 冒号后必须是"数字+可选空格+CJK"，避免误伤表格里的 `0.87:3mm` 这种数据。
    text = re.sub(r'(?m)^(\s*(?:#{1,6}\s+)?)(\d{1,2}\.\d{1,2})[:：](?=\d{1,2}(?![\d.])\s*[\u4e00-\u9fff])',
                  r'\1\2.', text)
    return text


def fix_ocr_artifacts(text):
    """修复常见 OCR 残留问题"""
    # 删除:全角括号→半角 是隐患规则。中文规范正文的( )全角括号
    # 是标准排版(数量极多),一旦跑起来会全部破坏,不启用。
    return text


def postprocess(text):
    """执行全部后处理"""
    text = fix_formula_spacing(text)
    text = fix_ocr_artifacts(text)
    text = fix_clause_number_formats(text)
    text = remove_header_footer_noise(text)
    text = cleanup_whitespace(text)
    return text


def main():
    if len(sys.argv) < 2:
        print("用法: python postprocess_md.py input.md [output.md]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file

    with open(input_file, 'r', encoding='utf-8') as f:
        original = f.read()

    # 统计修复前
    formulas_before = len(re.findall(r'\$[^$]+\$', original))
    gaps_before = len(re.findall(r'\d\s+\d', original))

    fixed = postprocess(original)

    # 统计修复后
    formulas_after = len(re.findall(r'\$[^$]+\$', fixed))
    gaps_after = len(re.findall(r'\d\s+\d', fixed))

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(fixed)

    print(f"✅ 后处理完成: {input_file} → {output_file}")
    print(f"   公式: {formulas_before} 个 ({formulas_after} 个修复)")
    print(f"   数字间距问题: {gaps_before} → {gaps_after} 处")
    print(f"   文件大小: {len(original)} → {len(fixed)} bytes ({len(fixed)-len(original):+d})")


if __name__ == '__main__':
    main()
