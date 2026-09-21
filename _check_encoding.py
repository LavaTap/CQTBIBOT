import zipfile

z = zipfile.ZipFile('沈昱作-技术美术实习生-可立即到岗.docx')
raw = z.read('word/document.xml')

# Try common encodings
for enc in ['utf-8', 'utf-16', 'utf-16le', 'utf-16be', 'gbk', 'gb2312']:
    try:
        xml = raw.decode(enc)
        print(f'{enc}: OK, len={len(xml)}')
        print(xml[:500])
        print('---')
        break
    except Exception as e:
        print(f'{enc}: {e}')
