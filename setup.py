from setuptools import setup

setup(
    name="syno-tools",
    version="0.0.1",
    description="Tools for Synology NAS",
    author="aubustou",
    author_email="survivalfr@yahoo.fr",
    install_requires=["requests", "pylast", "urllib3"],
    packages=["syno_tools"],
    entry_points={
        "console_scripts": [
            "similar = syno_tools.similar:main",
        ]
    },
    python_requires=">=3.9",
)
