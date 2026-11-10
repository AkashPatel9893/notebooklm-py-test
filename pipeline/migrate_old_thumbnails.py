#!/usr/bin/env python3
"""Migrate existing NCERT Drive resources to WebP thumbnails on demand.

Run without --apply for a read-only inventory. Originals are never deleted.
"""
import argparse
from pathlib import Path
import tempfile
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import drive_upload, mongo_sync, thumbnails, video_preview, mindmap_thumbnail

drive = None
resources = None
chapters = None

def download(file_id, dest):
    request = drive.svc.files().get_media(fileId=file_id)
    with dest.open('wb') as stream:
        worker = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = worker.next_chunk()

def upload(path, parent):
    existing = drive._find(path.name, parent, folder=False)
    digest = drive_upload.file_md5(path)
    if existing and existing.get('md5Checksum') == digest:
        file_id = existing['id']
    else:
        media = MediaFileUpload(str(path), mimetype='image/gif' if path.suffix.lower() == '.gif' else 'image/webp', resumable=True)
        if existing:
            request = drive.svc.files().update(fileId=existing['id'], media_body=media, fields='id')
        else:
            request = drive.svc.files().create(body={'name':path.name,'parents':[parent]},media_body=media,fields='id')
        response = None
        while response is None:
            _, response = request.next_chunk()
        file_id = response['id']
    drive.svc.permissions().create(fileId=file_id,body={'type':'anyone','role':'reader'},fields='id').execute()
    return drive_upload.links(file_id)['preview_url']

def main():
    global drive, resources, chapters
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Upload public WebP previews and update MongoDB')
    parser.add_argument('--slides-only', action='store_true', help='Process only slide decks')
    parser.add_argument('--video-only', action='store_true', help='Process only video previews')
    parser.add_argument('--mindmap-only', action='store_true', help='Process only mind-map previews')
    parser.add_argument('--chapter-id', help='Process only this MongoDB chapter ID')
    parser.add_argument('--video-root', type=Path, help='Local video files under <chapter ID>/cinematic_video.mp4')
    args = parser.parse_args()
    if sum((args.slides_only, args.video_only, args.mindmap_only)) > 1:
        parser.error('Choose only one of --slides-only, --video-only and --mindmap-only')
    if args.video_only and not args.video_root:
        parser.error('--video-only requires --video-root')
    apply = args.apply
    drive = drive_upload.Drive()
    db = mongo_sync.connect()
    resources = db[mongo_sync.C['resource']]
    chapters = db[mongo_sync.C['chapter']]
    if not apply:
        print('Read-only inventory; use --apply to migrate.')
    for doc in resources.find({}):
        items = doc.get('items', [])
        sources = {x['key']: x for x in items if x.get('key')}
        if args.chapter_id and str(doc.get('chapter_id')) != args.chapter_id:
            continue
        if args.video_only and not sources.get('cinematic_video'):
            continue
        mindmap = db[mongo_sync.C['mindmap']].find_one({'chapter_id': doc['chapter_id']}) if args.mindmap_only else None
        if args.mindmap_only and not (mindmap and mindmap.get('root')):
            continue
        if args.slides_only and not sources.get('slides', {}).get('drive_id'):
            continue
        if not apply:
            print('Chapter', str(doc['chapter_id']), 'resources:', ', '.join(sources), flush=True)
            continue
        source = sources.get('slides' if args.slides_only else 'source_pdf', {})
        if not source.get('drive_id'):
            print('Skipped chapter without a source PDF', flush=True)
            continue
        metadata = drive.svc.files().get(fileId=source['drive_id'], fields='parents').execute()
        if not metadata.get('parents'):
            print('Skipped chapter without a Drive folder', flush=True)
            continue
        parent = metadata['parents'][0]
        checked_sources = (source,) if args.slides_only or args.mindmap_only else (source, sources.get('slides', {}), sources.get('infographic', {}))
        for item in checked_sources:
            if not item.get('drive_id'):
                continue
            permissions = drive.svc.permissions().list(fileId=item['drive_id'], fields='permissions(type,role)').execute().get('permissions', [])
            if not any(p.get('type') == 'anyone' and p.get('role') == 'reader' for p in permissions):
                print('Skipped chapter: source is not public to link holders', flush=True)
                break
        else:
            permissions = None
        if permissions is not None:
            continue
        with tempfile.TemporaryDirectory(prefix='ncert-thumbnails-') as temp:
            folder = Path(temp)
            if args.mindmap_only:
                preview = mindmap_thumbnail.render(mindmap['root'], folder / 'mindmap_thumbnail.webp')
                url = upload(preview, parent)
                db[mongo_sync.C['mindmap']].update_one({'_id': mindmap['_id']}, {'$set': {'thumbnail_url': url}})
                print('Updated chapter', str(doc['chapter_id']), 'mind-map preview', flush=True)
                continue
            inputs = [] if args.video_only else [('slides','slides.pdf')] if args.slides_only else [('source_pdf','source.pdf'),('slides','slides.pdf'),('infographic','infographic.png')]
            for key, filename in inputs:
                item = sources.get(key, {})
                if item.get('drive_id'):
                    try: download(item['drive_id'], folder / filename)
                    except Exception as exc: print('Download failed:', key, type(exc).__name__, flush=True)
            video = sources.get('cinematic_video', {})
            generated = thumbnails.generate(folder)
            local_video = args.video_root / str(doc['chapter_id']) / 'cinematic_video.mp4' if args.video_root else None
            if not args.slides_only and video and local_video:
                try:
                    generated.append(video_preview.create_preview(local_video, folder / 'video_thumbnail.gif'))
                except (OSError, RuntimeError, ValueError) as exc:
                    print('Video preview unavailable:', str(exc), flush=True)
            links = {p.name: upload(p, parent) for p in generated}
            mapping = {'source_pdf':'chapter_thumbnail.webp','slides':'slides_page_1.webp','infographic':'infographic_thumbnail.webp','audio':'podcast_thumbnail.webp'}
            changes = []
            for key, name in mapping.items():
                if key not in sources or name not in links: continue
                fields = {'items.$[item].thumbnail_url': links[name]}
                if key == 'slides':
                    pages = sorted((name for name in links if name.startswith('slides_page_') and name.endswith('.webp')), key=lambda name: int(name.removeprefix('slides_page_').removesuffix('.webp')))
                    fields['items.$[item].thumbnail_urls'] = [links[name] for name in pages]
                result = resources.update_one({'_id':doc['_id']}, {'$set':fields}, array_filters=[{'item.key':key}])
                if result.matched_count: changes.append(key)
            if video and 'video_thumbnail.gif' in links:
                gif_link = links['video_thumbnail.gif']
                gif_id = gif_link.split('/file/d/')[1].split('/')[0]
                version = drive_upload.file_md5(folder / 'video_thumbnail.gif')[:12]
                resources.update_one({'_id': doc['_id']}, {'$set': {'items.$[item].thumbnail_animation_url': f'https://drive.google.com/uc?export=view&id={gif_id}&v={version}'}}, array_filters=[{'item.key': 'cinematic_video'}])
                changes.append('cinematic_video (animated)')
            if 'chapter_thumbnail.webp' in links:
                chapters.update_one({'_id':doc['chapter_id']},{'$set':{'thumbnail_url':links['chapter_thumbnail.webp']}})
            print('Updated chapter', str(doc['chapter_id']), 'resources:', ', '.join(changes), 'slide pages:', len([x for x in links if x.startswith('slides_page_')]), flush=True)

if __name__ == '__main__':
    main()
