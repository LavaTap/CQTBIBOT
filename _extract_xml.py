import zipfile
import re

z = zipfile.ZipFile('沈昱作-技术美术实习生-可立即到岗.docx')
xml = z.read('word/document.xml').decode('utf-8')
# Extract text from XML tags
text = re.sub(r'<[^>]+>', ' ', xml)
text = re.sub(r'\s+', ' ', text).strip()

with open('_resume_xml_text.txt', 'w', encoding='utf-8') as f:
    f.write(text)

print('XML text length:', len(text))
print(text[:2000])
