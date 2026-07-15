#!/bin/bash
pip install -r requirements.txt
THISDIR=$(pwd)
echo "alias teammcp=\"python $THISDIR/main.py\"" >> ~/.bashrc
echo -e "\033[31mPlease input \"source ~/.bashrc\" to flash env!!!!\033[0m"
