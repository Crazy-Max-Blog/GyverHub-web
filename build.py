import argparse
import sys
import os
import shutil
import gzip
import zipfile
import re
import base64
import json
import io
import html.parser

import rjsmin
import rcssmin

HERE = os.path.dirname(__file__)
SRCDIR = os.path.join(HERE, 'src')
BUILDDIR = os.path.join(HERE, 'build')
DISTDIR = os.path.join(HERE, 'dist')

RE_TAG_SINGLELINE = re.compile(r'(?:/\*|<!--)\s*@!\[(?P<tag>[^\]]*)\]\s*(?:\*/|-->)')
RE_TAG_MULTILINE = re.compile(r'(?:/\*|<!--)\s*@\[(?P<tag>[^\]]*)\]\s*(?:\*/|-->)(?P<data>(.|\n)*?)(?:/\*|<!--)\s*@/\[(?P<end_tag>[^\]]*)\]\s*(?:\*/|-->)', re.MULTILINE)


class PathResolver:
    def __init__(self, src_dir: str, build_dir: str, dist_dir: str):
        self.src_dir = src_dir
        self.build_dir = build_dir
        self.dist_dir = dist_dir

    def resolve(self, path: str, target: str, dist: bool = False):
        if dist:
            return os.path.join(self.dist_dir, path)

        if path.startswith('@'):
            return os.path.join(self.build_dir, target, path[1:])
        
        return os.path.join(self.src_dir, path)


class Compiler:
    def __init__(self, target: str, env: dict[str, str], pr: PathResolver):
        self._env = env.copy()
        self._env['target'] = target
        self._target = target
        self._resolver = pr

    def _resolve_read(self, path: str) -> bytes:
        print('  [R]', path)
        path = self._resolver.resolve(path, self._target)
        with open(path, 'rb') as f:
            return f.read()

    def _resolve_write(self, path: str, data: bytes):
        print('  [W]', path)
        path = self._resolver.resolve(path, self._target)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)

    def _include(self, path: str, options: list) -> bytes:
        data = self._resolve_read(path)

        if "compile" in options:
            data = self.compile_str(data.decode('utf-8')).encode('utf-8')

        if "js" in options:
            data = rjsmin.jsmin(data.decode('utf-8')).encode('utf-8')

        if "css" in options:
            data = rcssmin.cssmin(data.decode('utf-8')).encode('utf-8')

        if "html" in options:
            data = HtmlMinifier().minify(data.decode('utf-8')).encode('utf-8')

        if "json" in options:
            data = minify_json(data.decode('utf-8')).encode('utf-8')

        if "base64" in options:
            data = base64.b64encode(data)

        return data

    def _re_matched(self, match: re.Match[str]) -> str:
        d = match.groupdict()
        tag = d['tag']
        end_tag = d.get('end_tag')
        if end_tag is not None and end_tag != tag:
            raise RuntimeError(f'End tag does not match {tag!r} != {end_tag!r}')

        tag, _, args = tag.partition(':')
        tag = tag.strip()
        args = [i.strip() for i in args.split(',')]
        data = d.get('data', '')

        if tag == 'env' or tag == '':
            return self._env.get(args[0], '')
        
        if tag == 'if_target':
            return data if self._target in args else ''
        
        if tag == 'if_not_target':
            return '' if self._target in args else data

        if tag == 'include':
            return self._include(args[0], args[1:]).decode('utf-8')

        if tag == 'add_file':
            self.compile_file(args[0], options=args[1:])
            return ''

        if tag == 'add_file_to':
            self.compile_file(args[0], args[1], options=args[2:])
            return ''

        print('W: unknown tag:', tag)
        return ''

    def compile_str(self, source: str):
        source = RE_TAG_MULTILINE.sub(self._re_matched, source)
        source = RE_TAG_SINGLELINE.sub(self._re_matched, source)
        return source
    
    def compile_file(self, source: str, target: str = None, *, language: str = None, options: list = None):
        if options is None:
            options = ['compile']
        if target is None:
            target = '@' + source
        if language is None:
            options.append(os.path.splitext(source)[1][1:])
        else:
            options.append(language)
        
        data = self._include(source, options)
        self._resolve_write(target, data)


class HtmlMinifier(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._out = io.StringIO()

    @staticmethod
    def _attrv(v):
        if v is None:
            return v
        return f'="{html.escape(v)}"'
    
    @classmethod
    def _attrs(cls, attrs):
        if not attrs:
            return ''
        return ' ' + ' '.join((f'{k}{cls._attrv(v)}' for k, v in attrs))

    def get_value(self):
        return self._out.getvalue()

    def handle_startendtag(self, tag, attrs):
        self._out.write(f'<{tag}{self._attrs(attrs)}/>')

    def handle_starttag(self, tag, attrs):
        self._out.write(f'<{tag}{self._attrs(attrs)}>')

    def handle_endtag(self, tag):
        self._out.write(f'</{tag}>')

    def handle_charref(self, name):
        self._out.write(f'&#{name};')

    def handle_entityref(self, name):
        self._out.write(f'&{name};')

    def handle_data(self, data):
        self._out.write(data.strip())

    def handle_decl(self, decl):
        self._out.write(f'<!{decl}>')

    def minify(self, text):
        self.feed(text)
        self.close()
        return self.get_value()


def minify_json(text):
    return json.dumps(json.loads(text), ensure_ascii=False, separators=(',', ':'))


def pack_gzip(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, 'rb') as f_in, gzip.open(dst, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)


def pack_zip(srcdir, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in os.listdir(srcdir):
            zf.write(os.path.join(srcdir, i), i)


def file_to_h(src, dst, name, version):
    with open(src, "rb") as f:
        bytes = bytearray(f.read())

    data = '#pragma once\n'
    data += '// app v' + version + '\n\n'
    data += '#define ' + name + '_len ' + str(len(bytes)) + '\n\n'
    data += 'const uint8_t ' + name + '[] PROGMEM = {\n\t'

    count = 0
    for b in bytes:
        data += "0x{:02x}".format(b) + ', '
        count += 1
        if count % 16 == 0:
            data += '\n\t'

    data += '\n};'

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, 'wt', encoding='utf-8') as f:
        f.write(data)


class Builder:
    def __init__(self, env, pr: PathResolver):
        self._env = env
        self._resolver = pr
    
    def _compile(self, target: str, src: str, dst: str = None):
        Compiler(target, self._env, self._resolver).compile_file(src, dst)

    def _start_build(self, target):
        print(f'Building for target {target!r}...')

    def _build_zip(self, target, name):
        src = self._resolver.resolve('@.', target)
        dst = self._resolver.resolve(name, target, dist=True)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with zipfile.ZipFile(dst, 'w') as zf:
            for dirpath, dirnames, filenames in os.walk(src):
                for dirname in dirnames:
                    path = os.path.join(dirpath, dirname)
                    zf.write(path, os.path.relpath(path, src))
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    zf.write(path, os.path.relpath(path, src))

    def build_package(self, target: str, src: str, name: str, dst: str = None):
        self._start_build(target)
        self._compile(target, src, dst)
        self._build_zip(target, name)

    def build_direct(self, target: str, src: str, dst: str = None):
        self._start_build(target)
        temp = '@tempfile'
        self._compile(target, src, temp)
        
        src = self._resolver.resolve(temp, target)
        dst = self._resolver.resolve(dst, target, dist=True)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)

    def build_esp_gzip(self, target, src_target, name):
        self._start_build(target)
        src = self._resolver.resolve('@.', src_target)
        for dirpath, _, filenames in os.walk(src):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                dst = self._resolver.resolve('@' + os.path.relpath(path, src), target)
                pack_gzip(path, dst)

        self._build_zip(target, name)

    def build_esp_headers(self, target, src_target, name):
        self._start_build(target)
        version = self._env['version']

        src = self._resolver.resolve('@.', src_target)
        for dirpath, _, filenames in os.walk(src):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                dst = self._resolver.resolve('@' + os.path.relpath(path, src), target)
                basename = os.path.splitext(os.path.basename(path))[0]
                file_to_h(path, dst, f'hub_{basename}_h', version)
        
        self._build_zip(target, name)


def git_get_version():
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--automatic', help='Enable CI/github fixes', action='store_true')
    parser.add_argument('--version', help='Specify version', type=str)
    parser.add_argument('--next-version', help='Generate new version', action='store_true')
    parser.add_argument('--clean', help='Clean build files', action='store_true')
    parser.add_argument('--build', help='Build', action='store_true')

    args = parser.parse_args()

    if sum((args.next_version, args.clean, args.build)) != 1:
        print("You must specify exactly one of --build, --clean and --next-version!", file=sys.stderr)
        exit(1)
    
    if args.clean:
        shutil.rmtree(BUILDDIR, ignore_errors=True)
        shutil.rmtree(DISTDIR, ignore_errors=True)
        return
    
    if args.next_version:
        version = git_get_version()
        if version is None:
            with open(os.path.join(HERE, 'version.txt'), 'rt') as f:
                version = f.read().strip()
        
        beta = version.endswith('b')
        version = version.rstrip('b').split('.')
        version = [int(i) for i in version]
        version[-1] += 1
        version = '.'.join((str(i) for i in version))
        if beta:
            version += 'b'
        
        print(version)
        return

    env = {
        'version': args.version,
    }

    b = Builder(env, PathResolver(SRCDIR, BUILDDIR, DISTDIR))
    b.build_direct('lib', 'inc/lib/hub/index.js', 'GyverHub.min.js')
    b.build_package('host', 'index.html', 'host.zip')
    b.build_direct('mobile', 'index.html', 'mobile.html')
    b.build_direct('desktop', 'index.html', 'desktop.html')
    b.build_direct('local', 'index.html', 'GyverHub.html')
    b.build_package('esp', 'index.html', 'esp.zip')
    b.build_esp_gzip('esp-gz', 'esp', 'esp-gz.zip')
    b.build_esp_headers('esp-h', 'esp-gz', 'esp-headers.zip')


if __name__ == '__main__':
    main()
