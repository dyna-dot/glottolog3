"""
Project related model.
"""
from string import capwords

from zope.interface import implementer

from sqlalchemy import (
    Column,
    String,
    Boolean,
    Unicode,
    Integer,
    ForeignKey,
    desc,
    UniqueConstraint,
    and_,
    cast,
    Text,
    Index,
)
from sqlalchemy.orm import relationship, backref, joinedload
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.sql.expression import func

from clld.interfaces import ISource, ILanguage
from clld.db.meta import DBSession, Base, CustomModelMixin
from clld.db.models.common import (
    Language, Source, IdNameDescriptionMixin, IdentifierType, Identifier, Parameter,
)
from clld.util import DeclEnum

from glottolog3.interfaces import IProvider
from glottolog3.config import github


def get_parameter(pid):
    return DBSession.query(Parameter)\
        .filter(Parameter.id == pid)\
        .options(joinedload(Parameter.domain))\
        .one()


def get_source(key):
    return DBSession.query(Ref).join(Refprovider).filter(Refprovider.id == key).one()


@implementer(IProvider)
class Provider(Base, IdNameDescriptionMixin):
    """A provider of bibliographical data, i.e. entries in Ref.

    name -> id
    description -> name
    comment -> description
    """
    abbr = Column(Unicode)
    url = Column(Unicode)

    # if refurl is given, we can construct direct links to the provider's catalog ...
    refurl = Column(Unicode)
    # ... using the content of the bibfield attribute of a Ref instance.
    bibfield = Column(Unicode)

    @property
    def github_url(self):
        return github('references/bibtex/{0}.bib'.format(self.id))


class Doctype(Base, IdNameDescriptionMixin):
    """
    id -> pk
    id -> id
    abbr -> abbr
    name -> name
    """
    abbr = Column(Unicode)

    ord = Column(Integer)

    def __unicode__(self):
        return capwords(self.name.replace('_', ' '))


class Refdoctype(Base):
    __table_args__ = (UniqueConstraint('ref_pk', 'doctype_pk'),)
    doctype_pk = Column(Integer, ForeignKey('doctype.pk'), nullable=False)
    ref_pk = Column(Integer, ForeignKey('ref.pk'), nullable=False)


class Refprovider(Base):
    __table_args__ = (UniqueConstraint('ref_pk', 'provider_pk', 'id'),)
    provider_pk = Column(Integer, ForeignKey('provider.pk'), nullable=False)
    ref_pk = Column(Integer, ForeignKey('ref.pk'), nullable=False)
    id = Column(Unicode, unique=True, nullable=False)
    provider = relationship(Provider)

    @classmethod
    def get_stats(cls):
        return {
            row[0]: row[1] for row in
            DBSession.query(Provider.pk, func.count(cls.ref_pk).label('c'))
                .filter(Provider.pk == cls.provider_pk)
                .group_by(Provider.pk)
                .order_by(desc('c'))
                .all()}


#-----------------------------------------------------------------------------
# specialized common mapper classes
#-----------------------------------------------------------------------------
class LanguoidLevel(DeclEnum):
    family = 'family', 'family'
    language = 'language', 'language'
    dialect = 'dialect', 'dialect'


@implementer(ILanguage)
class Languoid(CustomModelMixin, Language):
    """
    id -> pk
    alnumcode -> id
    primaryname -> name
    names, codes -> languageidentifier
    refs -> languagesource
    """

    GLOTTOLOG_NAME = u'glottolog'

    pk = Column(Integer, ForeignKey('language.pk'), primary_key=True)

    # hid is the id of a language in Harald's classification. I.e. if hid is None, the
    # languoid is not a H-Language.
    hid = Column(Unicode, unique=True)

    father_pk = Column(Integer, ForeignKey('languoid.pk'))
    family_pk = Column(Integer, ForeignKey('languoid.pk'))

    level = Column(LanguoidLevel.db_type())
    bookkeeping = Column(Boolean, default=False)
    category = Column(Unicode)
    newick = Column(Unicode)

    child_family_count = Column(Integer)
    child_language_count = Column(Integer)
    child_dialect_count = Column(Integer)
    macroareas = Column(Unicode)

    descendants = relationship(
        'Languoid',
        order_by='Languoid.name, Languoid.id',
        foreign_keys=[family_pk],
        backref=backref('family', remote_side=[pk]))
    children = relationship(
        'Languoid',
        order_by='Languoid.name, Languoid.id',
        foreign_keys=[father_pk],
        backref=backref('father', remote_side=[pk]))

    def get_identifier_objs(self, type_):
        if getattr(type_, 'value', type_) == IdentifierType.glottolog.value:
            return [
                Identifier(name=self.id, type=IdentifierType.glottolog.value)]
        return Language.get_identifier_objs(self, type_)

    def get_ancestors(self, session=None):
        """
        :return: Iterable of ancestors of self back to the top-level family.
        """
        session = session or DBSession
        # retrieve the ancestors ordered by distance, i.e. from direct parent
        # to top-level family:
        return session.query(Languoid)\
            .join(TreeClosureTable, and_(
                TreeClosureTable.parent_pk == Languoid.pk,
                TreeClosureTable.depth > 0))\
            .filter(TreeClosureTable.child_pk == self.pk)\
            .order_by(TreeClosureTable.depth)

    @property
    def github_url(self):
        path = [self.id]
        for l in self.get_ancestors():
            path.append(l.id)
        return github('languoids/tree/{0}/md.ini'.format('/'.join(reversed(path))))

    def __json__(self, req=None, core=False):
        def ancestor(l):
            r = {"name": l.name, "id": l.id}
            if req:
                r['url'] = req.resource_url(l)
            return r
        res = super(Languoid, self).__json__(req)
        if not core:
            res['classification'] = [ancestor(l) for l in reversed(list(self.get_ancestors()))]
            if self.iso_code:
                res[IdentifierType.iso.value] = self.iso_code
            res['macroareas'] = {}#{ma.id: ma.name for ma in self.macroareas}
        return res

    def get_geocoords(self):
        """
        :return: sqlalchemy Query selecting quadrupels \
        (lid, primaryname, longitude, latitude) where lid is the Languoidbase.id of one\
        of the children of self.

        .. note::

            This method does not return the geo coordinates of the Languoid self, but of
            its descendants.
        """
        
        child_pks = DBSession.query(Languoid.pk)\
            .filter(Languoid.father_pk == self.pk).subquery()
        return DBSession.query(
            TreeClosureTable.parent_pk,
            Language.name,
            Language.longitude,
            Language.latitude,
            Language.id)\
            .filter(Language.pk == TreeClosureTable.child_pk)\
            .filter(TreeClosureTable.parent_pk.in_(child_pks))\
            .filter(Language.latitude != None)

    @property
    def valueset_dict(self):
        return {vs.parameter.id: vs for vs in self.valuesets}

    def classification(self, type_):
        assert type_ in ['fc', 'sc']
        return self.valueset_dict.get(type_)

    @property
    def fc(self):
        c = self.classification('fc')
        if c and c.description:
            return c

    @property
    def sc(self):
        c = self.classification('sc')
        if c and c.description:
            return c

    def _crefs(self, t):
        c = self.classification(t)
        if c:
            return list(c.references)
        return []

    @property
    def crefs(self):
        return sorted(
            self._crefs('fc') + self.screfs, key=lambda r: -(r.source.year_int or 0))

    @property
    def screfs(self):
        """
        The subclassification justification have a hereditary semantics. I.e.,
        if there is a reference to, e.g., Indo-European it justifies every child
        such as Germanic, Northwest Germanic and so on unless the child has its
        own justification (in which case that ref supersedes and takes over everything
        below). Suppose one is looking at a subfamily without its own
        explicit justification, then one should get the parent justification.
        """
        res = self._crefs('sc')
        if not res:
            if self.father:
                res = self.father.screfs
        return res

    def __rdf__(self, request):
        gold_type = None
        if self.level == LanguoidLevel.family:
            gold_type = 'LanguageSubfamily' if self.father_pk else 'LanguageFamily'
        elif self.level == LanguoidLevel.language:
            gold_type = 'Language'
        elif self.level == LanguoidLevel.dialect:
            gold_type = 'Dialect'
        if gold_type:
            yield 'rdf:type', 'http://purl.org/linguistics/gold/' + gold_type
        if self.family:
            yield 'skos:broaderTransitive', request.resource_url(self.family)
        if self.father:
            yield 'skos:broader', request.resource_url(self.father)
        for child in self.children:
            yield 'skos:narrower', request.resource_url(child)
        if not self.active:
            yield 'skos:changeNote', 'obsolete'
        for area in (self.macroareas or '').split(', '):
            yield 'dcterms:spatial', area

    def jqtree(self, icon_map=None):
        tree_ = []
        children_map = {}
        children_of_self = [c.pk for c in self.children]

        query = DBSession.query(
            Languoid.father_pk,
            Languoid.pk, Languoid.id, Languoid.name,
            Languoid.latitude, Languoid.hid,
            cast(Languoid.level, Text),
            Languoid.child_language_count, TreeClosureTable.depth)\
        .select_from(Languoid).join(TreeClosureTable,
            Languoid.pk == TreeClosureTable.child_pk)\
        .filter(TreeClosureTable.parent_pk == (self.family_pk or self.pk))\
        .order_by(TreeClosureTable.depth, Languoid.name)

        for row in query:
            fpk, cpk, id_, name, lat, hid, level, clc, depth = row
            if hid and len(hid) != 3:
                hid = None

            label = name
            if clc:
                label += ' (%s)' % clc
            #label = '%s [%s]' % (name, id_)
            #if level == 'language' and hid and len(hid) == 3:
            #    label += '[%s]' % hid
            node = {'id': id_, 'pk': cpk, 'iso': hid, 'level': level, 'label': label, 'children': []}
            if icon_map and id_ == self.id and lat:
                node['map_marker'] = icon_map[cpk]
            if cpk in children_of_self:
                node['child'] = True
                if icon_map and (level == 'family' or lat):
                    node['map_marker'] = icon_map[cpk]
            children_map[cpk] = node['children']

            if not fpk:
                tree_.append(node)
            else:
                if fpk not in children_map:
                    # this can be the case for dialects attached to inactive nodes
                    continue
                children_map[fpk].append(node)
        return tree_


# index datatables.Refs.default_order
source_order_index = Index('source_updated_desc_pk_desc_key',
    Source.updated.desc(), Source.pk.desc(), unique=True)


@implementer(ISource)
class Ref(CustomModelMixin, Source):
    """
    id -> pk
    bibtexkey -> id
    author + year -> name
    title -> description
    """
    pk = Column(Integer, ForeignKey('source.pk'), primary_key=True)
    fts = Column(TSVECTOR)

    field_labels = [
        ('author', 'author'),
        ('editor', 'editor'),
        ('year', 'year'),
        ('title', 'title'),
        ('address', 'city'),
        ('publisher', 'publisher'),
        ('pages', 'pages'),
        ('startpage', 'start page'),
        ('endpage', 'end page'),
        ('numberofpages', 'number of pages'),
        ('type', 'bibliographic type'),
        ('ozbib_id', 'OZBIB ID'),
    ]
    endpage_int = Column(Integer)
    inlg = Column(Unicode, index=True)
    inlg_code = Column(Unicode, index=True)
    subject = Column(Unicode)
    subject_headings = Column(Unicode)
    keywords = Column(Unicode)
    normalizedauthorstring = Column(Unicode)
    normalizededitorstring = Column(Unicode)
    ozbib_id = Column(Integer)
    language_note = Column(Unicode)
    srctrickle = Column(Unicode)

    gbid = Column(Unicode)
    iaid = Column(Unicode)

    #: store the trigger for computerized assignment of languages
    ca_language_trigger = Column(Unicode)

    #: store the trigger for computerized assignment of doctype
    ca_doctype_trigger = Column(Unicode)

    doctypes = relationship(
        Doctype,
        secondary=Refdoctype.__table__,
        order_by='Doctype.ord',
        backref=backref(
            'refs', order_by='Source.author, Source.year, Source.description'))

    macroareas = Column(Unicode)
    med_index = Column(Integer)
    med_pages = Column(Integer)
    med_type = Column(Unicode)

    providers = relationship(
        Provider,
        secondary=Refprovider.__table__,
        order_by='Provider.id',
        backref=backref(
            'refs', order_by='Source.author, Source.year, Source.description'))

    bibkeys = relationship(Refprovider, order_by='Refprovider.provider_pk')

    def __rdf__(self, request):
        """
        % for provider in ctx.providers:
        <dcterms:provenance rdf:parseType="Resource">
            <dcterms:description rdf:resource="${request.route_url('langdoc.meta', _anchor='provider-' + str(provider.id))}"/>
            <rdfs:label>${provider.description}</rdfs:label>
        </dcterms:provenance>
        % endfor

        % for ma in ctx.macroareas:
        <dcterms:spatial rdf:parseType="Resource">
            <dcterms:description rdf:resource="${request.route_url('home.glossary', _anchor='macroarea-'+str(ma.id))}"/>
            <rdfs:label>${ma.name}</rdfs:label>
        </dcterms:spatial>
        % endfor
        % for dt in ctx.doctypes:
        <dc:type rdf:parseType="Resource">
            <dcterms:description rdf:resource="${request.route_url('home.glossary', _anchor='doctype-'+str(dt.id))}"/>
            <rdfs:label>${dt.name}</rdfs:label>
        </dc:type>
        % endfor
        """

    def __bibtex__(self):
        res = {}
        for attr in 'inlg inlg_code subject subject_headings keywords ozbib_id'.split():
            v = getattr(self, attr, None)
            if v:
                res[attr] = '%s' % v
        return res


Refprovider.ref = relationship(Ref)


class TreeClosureTable(Base):
    __table_args__ = (UniqueConstraint('parent_pk', 'child_pk'),)
    parent_pk = Column(Integer, ForeignKey('languoid.pk'))
    child_pk = Column(Integer, ForeignKey('languoid.pk'))
    depth = Column(Integer)


class LegacyCode(Base):
    id = Column(String, unique=True)
    version = Column(String)

    def url(self, req):
        files = req.registry.settings['clld.files']
        page_url = str(files / 'glottolog-{0}'.format(self.version) / '{0}.html'.format(self.id))
        return req.static_url(page_url)
