# coding: utf-8
"""
管理端打包入口（薄包装，冻结入口之一）

EXE 只提供解释器与依赖；admin 逻辑一律以外置真实源码运行（与 core 同
策略，build.spec 把 admin/ 从 Analysis 的 pure/datas 过滤外置）。
启动后监听 6017 提供网页管理界面与 /api/v1/ 接口，不加载识别模型。
"""

import sys

from admin.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
