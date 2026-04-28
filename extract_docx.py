import zipfile
import os
import re

desktop = os.path.join(os.environ['USERPROFILE'], 'Desktop')
docx_path = os.path.join(desktop, '工作总结.docx')

with zipfile.ZipFile(docx_path, 'r') as z:
    xml_content = z.read('word/document.xml').decode('utf-8')
    text = re.sub(r'<[^>]+>', '', xml_content)
    
    # 保存到当前目录
    output_file = '工作总结内容.txt'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(text)
    
    print(f"内容已保存到 {output_file}")
    print(f"总长度：{len(text)} 字符")
    print(f"前 500 字符：{text[:500]}")
