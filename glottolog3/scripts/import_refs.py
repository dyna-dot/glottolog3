import sys
import re
import json
import random

import transaction

from sqlalchemy import desc, or_
from sqlalchemy.orm import joinedload
from clld.lib.bibtex import Database, EntryType
from clld.util import UnicodeMixin, slug
from clld.scripts.util import parsed_args
from clld.db.meta import DBSession
from clld.db.models.common import Source

from glottolog3.lib.util import roman_to_int
from glottolog3.lib.bibtex import unescape
from glottolog3.models import (
    Ref, Provider, Refprovider, Macroarea, Doctype, Country, Languoid,
)
from glottolog3.lib.util import get_map
from glottolog3.scripts.util import update_providers, update_relationship, update_reflang, compute_pages

# id
# bibtexkey
# type
# startpage              | integer           |
# endpage                | integer           |
# numberofpages          | integer           |

# bibtexkey              | text              | not null
# type                   | text              | not null
# inlg_code              | text              |
# year                   | integer           |
# jsondata               | character varying |

FIELD_MAP = {
    'abstract': '',
    'added': '',
    'additional_items': '',
    'address': 'address',
    'adress': 'address',
    'adviser': '',
    'aiatsis_callnumber': '',
    'aiatsis_code': '',
    'aiatsis_reference_language': '',
    'alnumcodes': '',
    'anlanote': '',
    'anlclanguage': '',
    'anlctype': '',
    'annote': '',
    'asjp_name': '',
    'audiofile': '',
    'author': 'author',
    'author_note': '',
    'author_statement': '',
    'booktitle': 'booktitle',
    'booktitle_english': '',
    'bwonote': '',
    'call_number': '',
    'citation': '',
    'class_loc': '',
    'collection': '',
    'comments': '',
    'contains_also': '',
    'contributed': '',
    'copies': '',
    'copyright': '',
    'country': '',
    'coverage': '',
    'crossref': '',
    'de': '',
    'degree': '',
    'digital_formats': '',
    'document_type': '',
    'doi': '',
    'domain': '',
    'edition': 'edition',
    'edition_note': '',
    'editor': 'editor',
    'english_title': '',
    'extra_hash': '',
    'extrahash': '',
    'file': '',
    'fn': '',
    'fnnote': '',
    'folder': '',
    'format': '',
    'german_subject_headings': '',
    'glottolog_ref_id': '',
    'guldemann_location': '',
    'hhnote': '',
    'hhtype': '',
    'howpublished': '',
    'id': '',
    'inlg': 'inlg',
    'institution': '',
    'isbn': '',
    'issn': '',
    'issue': '',
    'jfmnote': '',
    'journal': 'journal',
    'key': '',
    'keywords': '',
    'langcode': '',
    'langnote': '',
    'languoidbase_ids': '',
    'lapollanote': '',
    'last_changed': '',
    'lccn': '',
    'lcode': '',
    'lgcde': '',
    'lgcode': '',
    'lgcoe': '',
    'lgcosw': '',
    'lgfamily': '',
    'macro_area': '',
    'modified': '',
    'month': '',
    'mpi_eva_library_shelf': '',
    'mpifn': '',
    'no_inventaris': '',
    'note': 'note',
    'notes': 'note',
    'number': 'number',
    'numberofpages': '',
    'numner': 'number',
    'oages': 'pages',
    'oldhhfn': '',
    'oldhhfnnote': '',
    'omnote': '',
    'other_editions': '',
    'otomanguean_heading': '',
    'owner': '',
    'ozbib_id': 'ozbib_id',
    'ozbibnote': '',
    'ozbibreftype': '',
    'paged': 'pages',
    'pages': 'pages',
    'pagex': 'pages',
    'permission': '',
    'pgaes': 'pages',
    'phdthesis': '',
    'prepages': '',
    'publisher': 'publisher',
    'pubnote': '',
    'rating': '',
    'read': '',
    'relatedresource': '',
    'replication': '',
    'reprint': '',
    'restrictions': '',
    'review': '',
    'school': 'school',
    'seanote': '',
    'seifarttype': '',
    'series': 'series',
    'series_english': '',
    'shelf_location': '',
    'shorttitle': '',
    'sil_id': '',
    'source': '',
    'src': '',
    'srctrickle': '',
    'stampeann': '',
    'stampedesc': '',
    'status': '',
    'subject': 'subject',
    'subject_headings': 'subject_headings',
    'subsistence_note': '',
    'superseded': '',
    'thanks': '',
    'thesistype': '',
    'timestamp': '',
    'title': 'title',
    'title_english': '',
    'titlealt': '',
    'typ': '',
    'umi_id': '',
    'url': 'url',
    'vernacular_title': '',
    'volume': 'volume',
    'volumr': 'volume',
    'weball_lgs': '',
    'year': 'year',
    'yeartitle': '',
}

CONVERTER = {'ozbib_id': int}

PREF_YEAR_PATTERN = re.compile('\[(?P<year>(1|2)[0-9]{3})(\-[0-9]+)?\]')
YEAR_PATTERN = re.compile('(?P<year>(1|2)[0-9]{3})')
ROMAN = '(?P<roman>[ivxlcdmIVXLCDM]+)'
ARABIC = '(?P<arabic>[0-9]+)'
ROMANPAGESPATTERNra = re.compile(u'%s\+%s' % (ROMAN, ARABIC))
ROMANPAGESPATTERNar = re.compile(u'%s\+%s' % (ARABIC, ROMAN))
DOCTYPE_PATTERN = re.compile('(?P<name>[a-z\_]+)\s*(\((?P<comment>[^\)]+)\))?\s*(\;|$)')
CODE_PATTERN = re.compile('\[(?P<code>[^\]]+)\]')


#
# TODO: implement three modes: compare, import, update
#

def main(args):  # pragma: no cover
    bib = Database.from_file(args.data_file(args.version, 'refs.bib'), encoding='utf8')
    mode = args.mode

    count = 0
    skipped = 0

    changes = {}

    with transaction.manager:
        update_providers(args)
        DBSession.flush()
        provider_map = get_map(Provider)
        macroarea_map = get_map(Macroarea)
        doctype_map = get_map(Doctype)

        known_ids = set(r[0] for r in DBSession.query(Ref.pk))

        languoid_map = {}
        for l in DBSession.query(Languoid):
            if l.hid:
                languoid_map[l.hid] = l
            languoid_map[l.id] = l

        for i, rec in enumerate(bib):
            if len(rec.keys()) < 6:
                skipped += 1
                #print '---> skip', rec.id
                #print rec
                continue

            changed = False
            assert rec.get('glottolog_ref_id')
            id_ = int(rec.get('glottolog_ref_id'))
            if mode != 'update' and id_ in known_ids:
                continue
            ref = DBSession.query(Source).get(id_)
            update = True if ref else False

            kw = {
                'pk': id_,
                'bibtex_type': rec.genre,
                'id': str(id_),
                'jsondata': {'bibtexkey': rec.id},
            }

            for source, target in FIELD_MAP.items():
                value = rec.get(source)
                if value:
                    value = unescape(value)
                    if target:
                        kw[target] = CONVERTER.get(source, lambda x: x)(value)
                    else:
                        kw['jsondata'][source] = value

            # try to extract numeric year, startpage, endpage, numberofpages, ...
            if rec.get('numberofpages'):
                try:
                    kw['pages_int'] = int(rec.get('numberofpages').strip())
                except ValueError:
                    pass

            if kw.get('year'):
                #
                # prefer years in brackets over the first 4-digit number.
                #
                match = PREF_YEAR_PATTERN.search(kw.get('year'))
                if match:
                    kw['year_int'] = int(match.group('year'))
                else:
                    match = YEAR_PATTERN.search(kw.get('year'))
                    if match:
                        kw['year_int'] = int(match.group('year'))

            if kw.get('publisher'):
                p = kw.get('publisher')
                if ':' in p:
                    address, publisher = [s.strip() for s in kw['publisher'].split(':', 1)]
                    if not 'address' in kw or kw['address'] == address:
                        kw['address'], kw['publisher'] = address, publisher

            if kw.get('pages'):
                pages = kw.get('pages')
                match = ROMANPAGESPATTERNra.search(pages)
                if not match:
                    match = ROMANPAGESPATTERNar.search(pages)
                if match:
                    if 'pages_int' not in kw:
                        kw['pages_int'] = roman_to_int(match.group('roman')) \
                            + int(match.group('arabic'))
                else:
                    start, end, number = compute_pages(pages)
                    if start is not None:
                        kw['startpage_int'] = start
                    if end is not None:
                        kw['endpage_int'] = end
                    if number is not None:
                        kw['pages_int'] = number

            if update:
                for k in kw.keys():
                    if k == 'pk':
                        continue
                    #if k == 'title':
                    #    v = ref.title or ref.description
                    #else:
                    if 1:
                        v = getattr(ref, k)
                    if kw[k] != v:
                        if k == 'jsondata':
                            ref.update_jsondata(**kw[k])
                        else:
                            print k, '--', v
                            print k, '++', kw[k]
                            setattr(ref, k, kw[k])
                            changed = True
                            if ref.id in changes:
                                changes[ref.id][k] = ('%s' % v, '%s' % kw[k])
                            else:
                                changes[ref.id] = {k: ('%s' % v, '%s' % kw[k])}
                    if ref.title:
                        ref.description = ref.title
            else:
                changed = True
                ref = Ref(name='%s %s' % (kw.get('author', 'na'), kw.get('year', 'nd')), **kw)

            def append(attr, obj):
                if obj and obj not in attr:
                    attr.append(obj)
                    return True

            a, r = update_relationship(
                ref.macroareas,
                [macroarea_map[name] for name in
                 set(filter(None, [s.strip() for s in kw['jsondata'].get('macro_area', '').split(',')]))])
            changed = changed or a or r
            #for name in set(filter(None, [s.strip() for s in kw['jsondata'].get('macro_area', '').split(',')])):
            #    result = append(ref.macroareas, macroarea_map[name])
            #    changed = changed or result

            for name in set(filter(None, [s.strip() for s in kw['jsondata'].get('src', '').split(',')])):
                result = append(ref.providers, provider_map[slug(name)])
                changed = changed or result

            a, r = update_relationship(
                ref.doctypes,
                [doctype_map[m.group('name')] for m in
                 DOCTYPE_PATTERN.finditer(kw['jsondata'].get('hhtype', ''))])
            changed = changed or a or r
            #for m in DOCTYPE_PATTERN.finditer(kw['jsondata'].get('hhtype', '')):
            #    result = append(ref.doctypes, doctype_map[m.group('name')])
            #    changed = changed or result

            if len(kw['jsondata'].get('lgcode', '')) == 3:
                kw['jsondata']['lgcode'] = '[%s]' % kw['jsondata']['lgcode']

            #for m in CODE_PATTERN.finditer(kw['jsondata'].get('lgcode', '')):
            #    for code in set(m.group('code').split(',')):
            #        if code not in languoid_map:
            #            if code not in ['NOCODE_Payagua', 'emx']:
            #                print '--> unknown code:', code.encode('utf8')
            #        else:
            #            result = append(ref.languages, languoid_map[code])
            #            changed = changed or result

            #for glottocode in filter(None, kw['jsondata'].get('alnumcodes', '').split(';')):
            #    if glottocode not in languoid_map:
            #        print '--> unknown glottocode:', glottocode.encode('utf8')
            #    else:
            #        result = append(ref.languages, languoid_map[glottocode])
            #        changed = changed or result

            if not update:
                DBSession.add(ref)

            if changed:
                count += 1
                ref.doctypes_str = ', '.join(o.id for o in ref.doctypes)
                ref.providers_str = ', '.join(o.id for o in ref.providers)

            if i % 1000 == 0:
                print i, 'records done', count, 'changed'

        print count, 'records updated or imported'
        print skipped, 'records skipped because of lack of information'
        update_reflang(args)

    return changes


if __name__ == '__main__':
    args = parsed_args(
        (('--mode',), dict(default='insert')),
        (("--version",), dict(default="2.0")),
    )
    res = main(args)
    with open(args.data_file(args.version, 'refs.json'), 'w') as fp:
        json.dump(res, fp)
