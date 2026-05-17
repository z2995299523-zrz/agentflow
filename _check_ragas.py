try:
    import ragas
    print("RAGAS", ragas.__version__)
except ImportError:
    print("NOT INSTALLED")
