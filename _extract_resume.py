from docx import Document

doc = Document('沈昱作-技术美术实习生-可立即到岗.docx')
full_text = []
for para in doc.paragraphs:
    text = para.text.strip()
    if text:
        full_text.append(text)
    else:
        runs_text = ''.join(r.text for r in para.runs).strip()
        if runs_text:
            full_text.append(runs_text)

with open('_resume_text.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(full_text))

print(f'Extracted {len(full_text)} paragraphs')
