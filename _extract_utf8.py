import zipfile
import xml.etree.ElementTree as ET

z = zipfile.ZipFile('沈昱作-技术美术实习生-可立即到岗.docx')
xml = z.read('word/document.xml')
root = ET.fromstring(xml)

texts = []
for t in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
    if t.text:
        texts.append(t.text)

full_text = ''.join(texts)

with open('_resume_utf8.txt', 'w', encoding='utf-8') as f:
    f.write(full_text)

print('Extracted length:', len(full_text))
