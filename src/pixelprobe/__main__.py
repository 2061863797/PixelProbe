"""支持模块启动及冻结程序的工作进程入口。"""

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from pixelprobe.cli.app import main
    main()
