"""Tải font Be Vietnam Pro (Google Fonts, giấy phép SIL OFL 1.1) để LibreOffice dựng ảnh slide PowerPoint đúng thiết kế.

    python scripts/download_fonts.py --dest /usr/local/share/fonts/google     # Dockerfile gọi lúc build image

Các font hay gặp khác (Liberation thay Arial / Times New Roman, Carlito thay Calibri, Roboto, Open Sans, Lato, Montserrat,
Inter...) cài bằng apt trong Dockerfile. Bản font cố định theo commit của github.com/google/fonts và kiểm tra sha256 từng
tệp: build lại vẫn ra đúng các tệp này.
"""
import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/google/fonts/c13390277dfb1fcc7415d84ef7ef9cfc1e52f8c1/ofl/bevietnampro/"
FILES = {
    "BeVietnamPro-Thin.ttf": "3ff7cae0bdd4b2087fe24583d1137dc0a6ecfed96c62ee39955992fa1629176c",
    "BeVietnamPro-ThinItalic.ttf": "14c50e3d6c8980656681ede48068d12ae645c71b1eaa40ff14267d54a49c5c5a",
    "BeVietnamPro-ExtraLight.ttf": "3df1650399fd891967082bd2f78e1d87fe4eb82fc7bdbb7ab94ba8bda848caa1",
    "BeVietnamPro-ExtraLightItalic.ttf": "fc4410f0aa39430463f103463d27be5486c2018d11854f1733dd27be7348b41c",
    "BeVietnamPro-Light.ttf": "f0b7143edac3fd99960312706cfa51cbafec28e7afa6d4afbd08a73bb246af13",
    "BeVietnamPro-LightItalic.ttf": "baeadf148ee2b6640d7910ebab99b01aaaf5eb009463bdf466ab2b43108389fd",
    "BeVietnamPro-Regular.ttf": "cd1ef6e9d7db28ad5cdb88a65ccbe693870e60d340b791f349d248342b4fe4c3",
    "BeVietnamPro-Italic.ttf": "a33ebb3f68e0e260a7d3874d64259ad309971a2daf969e7ce5f444d70bce535f",
    "BeVietnamPro-Medium.ttf": "b60832bfa0fcd015158112c64d7e3fdad3b0c6287d1823f85a3103636e845268",
    "BeVietnamPro-MediumItalic.ttf": "b7d43a5f943048f7d4e49a080052acce1902f1eab07f4160299d84abab8119fa",
    "BeVietnamPro-SemiBold.ttf": "bd8e27eb02720b9d91e59e4f10a90878643219f25ce6a8d9a4f06a8a88d3bb71",
    "BeVietnamPro-SemiBoldItalic.ttf": "f7dbf8a63f4b4a6f7d7f1f4f27443495d01475663bb8fe5cb1a6fc6740e82a27",
    "BeVietnamPro-Bold.ttf": "7f738fe5c43c8872807b20e2d30d42163618de8a4daf7f48a939adac32c16847",
    "BeVietnamPro-BoldItalic.ttf": "a2a65bdf9b251beb3d658ae691889d71ce63c0290940c87ca60b449ceb0bdaee",
    "BeVietnamPro-ExtraBold.ttf": "44dbfa01553d227bec76ba6446c5618870a76a4a3297f41630dde859d0650d02",
    "BeVietnamPro-ExtraBoldItalic.ttf": "b5544a50b74c3153f7aa68614cfaebbea924869e6d6fdfd4ec80decfdd7ff4cd",
    "BeVietnamPro-Black.ttf": "dd0ec461782da7f4a58698b431a67c455ced7c09deb9c40d7b47e61975d3ffdb",
    "BeVietnamPro-BlackItalic.ttf": "d55266c614d1fb013a6661c5cd1d98f31f2e03418d83e90f509b210f452c0cdf",
    "OFL.txt": "6b7f8f73609a25ea78c891e34cf37b06f8a676b7ea986e941e43b009110f2a85",     # giấy phép đi kèm font
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Tải font Be Vietnam Pro cho LibreOffice")
    ap.add_argument("--dest", required=True, help="thư mục font, ví dụ /usr/local/share/fonts/google")
    dest = Path(ap.parse_args().dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, sha in FILES.items():
        with urllib.request.urlopen(BASE + name, timeout=60) as r:
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            print(f"Sai sha256 của {name}: {got}", file=sys.stderr)
            return 1
        (dest / ("BeVietnamPro-OFL.txt" if name == "OFL.txt" else name)).write_bytes(data)
    print(f"Đã tải {len(FILES)} tệp vào {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
