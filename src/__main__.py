
if __name__ == '__main__':
    from pathlib import Path
    path = Path(__file__).parent
   
    import sys
    sys.path.insert(0, path)
    
    from . import laia as laia
    laia.main()
