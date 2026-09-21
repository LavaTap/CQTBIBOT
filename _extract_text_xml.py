import zipfile
import xml.etree.ElementTree as ET

z = zipfile.ZipFile('沈昱作-技术美术实习生-可立即到岗.docx')
xml = z.read('word/document.xml')
root = ET.fromstring(xml)

ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
texts = []
for t in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
    if t.text:
        texts.append(t.text)

full_text = ''.join(texts)
print('Text length:', len(full_text))
print(full_text[:3000])
print('\n...')
print(full_text[-1000:])
