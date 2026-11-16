import os
from image_lab import create_app
from tileatlas import install
from waitress import serve
root = os.environ.get('TILEATLAS_DATA_DIR', 'data')
app = create_app(os.path.join(root, 'preparation'))
install(app, os.path.join(root, 'atlases'))
if __name__ == '__main__':
    serve(app, host=os.environ.get('HOST','0.0.0.0'),port=int(os.environ.get('PORT','8914')),threads=4)
