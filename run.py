"""Start the integrated kiosk and API on one local origin."""
import argparse
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    print(f'Open http://127.0.0.1:{args.port} after startup completes.')
    uvicorn.run('api.main:app', host='127.0.0.1', port=args.port)
