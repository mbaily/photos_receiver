#!/usr/bin/env python3
"""Build Send Batch to Server using unqforge, with the local upload token embedded.

Run ./build_shortcut.sh. Outputs are private, ignored files in shortcut_downloads/.
The generated shortcut requires Apple signing before importing on iOS.
"""
import os
from pathlib import Path
import plistlib
import sys

from unqforge import SC, E, att, ts, var

A = 'is.workflow.actions.'
SERVER = 'https://photos.example.com:9443/iphone_photos'


def build(token):
    s = SC()
    s.meta = {
        'WFWorkflowName': 'Send Batch to Server',
        'WFWorkflowTypes': ['ActionExtension'],
        'WFWorkflowHasShortcutInputVariables': True,
        'WFWorkflowInputContentItemClasses': [
            'WFImageContentItem', 'WFPhotoMediaContentItem',
            'WFAVAssetContentItem', 'WFGenericFileContentItem'],
    }

    def headers(**pairs):
        return {'Value': {'WFDictionaryFieldValueItems': [
            {'WFItemType': 0, 'WFKey': ts(k.replace('_', '-')),
             'WFValue': v if isinstance(v, dict) else ts(v)}
            for k, v in pairs.items()]},
            'WFSerializationType': 'WFDictionaryFieldValue'}

    source = {'Type': 'ExtensionInput'}
    count = s.action(A + 'count', Input=att(source))
    with s.if_(count, '==', 0):
        s.action(A + 'exit')
    s.setvar('Token', token)
    s.setvar('Uploaded', 0)
    auth = ts('Bearer ', var('Token'))
    # Repeat Results preserves one digest per input, in the same order.
    with s.foreach(source) as hashing:
        s.action(A + 'hash', WFHashType='SHA256', WFInput=att(hashing.item))
    s.setvar('Hashes', hashing.results)
    joined = s.action(A + 'text.combine', text=att(var('Hashes')),
                      WFTextSeparator='Custom', WFTextCustomSeparator='","')
    body = s.text(ts('{"hashes":["', joined, '"]}'))
    reply = s.action(A + 'downloadurl', WFURL=ts(SERVER, '/wanted'),
                     WFHTTPMethod='POST', WFHTTPBodyType='File',
                     WFRequestVariable=att(body), ShowHeaders=True,
                     WFHTTPHeaders=headers(Content_Type='application/json', Authorization=auth))
    parsed = s.action(A + 'detect.dictionary', WFInput=att(reply))
    wanted = s.getval(parsed, 'wanted')
    # Skip the entire upload loop when the server already has everything.
    with s.if_has_value(wanted):
        wanted_text = s.action(A + 'text.combine', text=att(wanted),
                               WFTextSeparator='New Lines')
        with s.foreach(source) as it:
            digest = s.action(A + 'getitemfromlist', WFInput=att(var('Hashes')),
                              WFItemSpecifier='Item At Index', WFItemIndex=att(it.index))
            match = s.action(A + 'text.match', text=ts(wanted_text),
                             WFMatchTextPattern=ts('(?m)^', digest, '$'),
                             WFMatchTextCaseSensitive=True)
            with s.if_has_value(match):
                s.action(A + 'downloadurl', WFURL=ts(SERVER, '/', digest),
                         WFHTTPMethod='PUT', WFHTTPBodyType='File',
                         WFRequestVariable=att(it.item), ShowHeaders=True,
                         WFHTTPHeaders=headers(Authorization=auth))
                s.setvar('Uploaded', s.calc(E(var('Uploaded')) + 1))
    s.action(A + 'notification', WFNotificationActionTitle='Photos',
             WFNotificationActionBody=ts(var('Uploaded'), ' uploaded'))
    return s


def main():
    token = os.environ.get('PHOTO_TOKEN', '').strip()
    if not token:
        token = Path.home().joinpath('.config/photo_token').read_text().strip()
    if not token or '\n' in token or '\r' in token:
        raise SystemExit('A nonempty, single-line upload token is required')
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else 'shortcut_downloads')
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    s = build(token)
    s.dump(dest / 'Send Batch to Server.plist')
    with (dest / 'Send Batch to Server.unsigned.shortcut').open('wb') as f:
        plistlib.dump(s.plist(), f, fmt=plistlib.FMT_BINARY)
    print(f'Built {len(s.acts)} actions in {dest}; Apple signing is still required.')


if __name__ == '__main__':
    main()
