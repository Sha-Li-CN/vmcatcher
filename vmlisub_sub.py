#!/usr/bin/env python
from sqlalchemy import create_engine
import hepixvmlis.databaseDefinition as model

import logging
import optparse
import smimeX509validation.loadcanamespace as loadcanamespace
import sys
from hepixvmlis.__version__ import version

from sqlalchemy.orm import sessionmaker
import hepixvmlis
import urllib2
import urllib
import hashlib
import datetime
from hepixvmitrust.vmitrustlib import VMimageListDecoder as VMimageListDecoder
from hepixvmitrust.vmitrustlib import time_format_definition as time_format_definition
try:
    import json
except:
    import simplejson

class db_actions:
    
    def __init__(self,session):
        self.session = session
        self.log = logging.getLogger("db_actions")
    def endorser_get(self,metadata):
        return self.session.query(model.Endorser).\
                filter(model.Endorser.id==model.EndorserPrincible.id).\
                filter(model.EndorserPrincible.hv_dn==metadata[u'hv:dn']).\
                filter(model.EndorserPrincible.hv_ca==metadata[u'hv:ca'])
    def endorser_create(self,metadata):
        gotquery = self.endorser_get(metadata)
        if gotquery.count() != 0:
            return gotquery
        newlist = model.Endorser(metadata)
        self.session.add(newlist)
        self.session.commit()
        new_endorser = model.EndorserPrincible(newlist.id,metadata)
        
        self.session.add(new_endorser)
        self.session.commit()
        return self.endorser_get(metadata)
    def subscription_get(self,metadata):
        subscriptionlist = self.session.query(model.Subscription).\
                filter(model.Subscription.url==metadata[u'hv:uri'])
        return subscriptionlist
        
    def subscription_create(self,metadata,authorised=True):
        subscription_query = self.subscription_get(metadata)
        if subscription_query.count() != 0:
            return subscription_query
        endorser_list = self.endorser_create(metadata)
        endorser = endorser_list.one()
        new_subscription = model.Subscription(metadata)
        # We will make the new subscription enabled by default
        new_subscription.authorised = True
        self.session.add(new_subscription)
        self.session.commit()
        new_auth = model.SubscriptionAuth(new_subscription.id,endorser.id,authorised)
        self.session.add(new_auth)
        self.session.commit()
        return self.subscription_get(metadata)
        
    def subscribe_file(self,anchor,filename):
        req = urllib2.Request(url=filename) 
        f = urllib2.urlopen(req)
        validated_data = anchor.validate_text(f.read())
        jsontext = json.loads(validated_data['data'])
        vmilist = VMimageListDecoder(jsontext)
        
        metadata = {}
        metadata.update(vmilist.metadata)
        metadata.update(vmilist.endorser.metadata)
        if u'dc:identifier' not in metadata.keys():
            self.log.error('list dc:identifier does not found')
            return False
        if metadata[u'hv:dn'] != validated_data['signer_dn']:
            self.log.error('Endorser DN does not match signature')
            return False
        if metadata[u'hv:ca'] != validated_data['issuer_dn']:
            self.log.error('list hv:ca does not match signature')
            return False
        if metadata[u'hv:uri'] != filename:
            self.log.warning('list hv:uri does not match subscription uri')
        
            
        self.subscription_create(metadata,authorised=True)

    


    def subscriptions_update(self,anchor):
        
        subscriptionlist = self.session.query(model.Subscription).all()
        for subscription in subscriptionlist:            
            self.log.info("Updating:%s" % (subscription.uuid))
            req = urllib2.Request(url=subscription.url)
            f = urllib2.urlopen(req)
            update_unprocessed = f.read()
            # Now we have the update lets first check its hash 
            messagehash = hashlib.sha512(update_unprocessed).hexdigest()
            messagehash_q = self.session.query(model.Imagelist).\
                filter(model.Imagelist.data_hash==messagehash)
            count = messagehash_q.count()
            if count != 0:
                self.log.debug('Hash already found')
                continue
            #Now we check its authenticity
            validated_data = anchor.validate_text(update_unprocessed)
            data = validated_data['data']
            dn = validated_data['signer_dn']
            ca = validated_data['issuer_dn']
            jsontext = json.loads(data)
            vmilist = VMimageListDecoder(jsontext)
            
            removeauthorsiation = False
            
            if vmilist.endorser.metadata[u'hv:dn'] != dn:
                self.log.error('Endorser DN does not match signature')
                continue
            if vmilist.endorser.metadata[u'hv:ca'] != ca:
                self.log.error( 'list hv:ca does not match signature')
                continue
            if vmilist.metadata[u'hv:uri'] != subscription.url:
                self.log.error('list hv:uri does not match subscription uri')
                continue
            if vmilist.metadata[u'dc:identifier'] != subscription.uuid:
                self.log.error('list dc:identifier does not match subscription uuid')
                continue
            now = datetime.datetime.utcnow()
            if now < vmilist.metadata[u'dc:date:created']:
                self.log.error('Invalide creation date:%s' % (subscription.uuid))
                continue
            if now > vmilist.metadata[u'dc:date:expires']:
                self.log.warning('Image list has expired:%s' % (subscription.uuid))
                removeauthorsiation = True
            metadata = vmilist.metadata
            metadata[u'data'] = update_unprocessed
            metadata[u'data-hash'] = messagehash
            if removeauthorsiation:
                metadata[u'authorised'] = False
            
            # Now we know the data better check the SubscriptionAuth
            subauthq = self.session.query(model.SubscriptionAuth).\
                filter(model.Endorser.id==model.EndorserPrincible.id).\
                filter(model.EndorserPrincible.hv_dn==dn).\
                filter(model.EndorserPrincible.hv_ca==ca).\
                filter(model.SubscriptionAuth.endorser == model.Endorser.id).\
                filter(model.SubscriptionAuth.subscription == model.Subscription.id).\
                filter(model.Subscription.id == subscription.id)
                
            count = subauthq.count()
            if count == 0:
                self.log.error('Endorser not authorised on subscription')
                return False
            authsub = subauthq.one()
            
            imagelist = model.Imagelist(authsub.id,metadata)
            self.session.add(imagelist)
            self.session.commit()
            for imageObj in vmilist.images:
                imageDb = model.Image(imagelist.id,imageObj.metadata)
                self.session.add(imageDb)
                self.session.commit()
            if subscription.imagelist_latest != None:
                oldimagelist_q = self.session.query(model.Imagelist).\
                    filter(model.Imagelist.id==imagelist_latest)
                for imagelist in oldimagelist_q:
                    imagelist.authorised = False

            subscription.updated = datetime.datetime.utcnow()   
            subscription.imagelist_latest = imagelist.id
            self.session.commit()
    def subscriptions_delete(self,uuid):
        subscriptionlist = self.session.query(model.Subscription).\
            filter(model.Subscription.uuid==uuid)
        for item in subscriptionlist:
            #print item.SubscriptionAuth
            self.session.delete(item)
        self.session.commit()
        return




class queryby_base:
    """"Base class for querying subscriptions"""
    def __init__(self,session):
        self.session = session
    def subscription_by_id(self,private_id):
        subscriptionlist = self.session.query(model.Subscription).\
                filter(model.Subscription.id==private_id)
        return subscriptionlist
    def subscription_by_uri(self,url):
        subscriptionlist = self.session.query(model.Subscription).\
                filter(model.Subscription.url==url)
        return subscriptionlist
    def subscription_by_uuid(self,uuid):
        subscriptionlist = self.session.query(model.Subscription).\
                filter(model.Subscription.uuid==uuid)
        return subscriptionlist
    def imagelist_by_id(self,private_id):
        subscriptionlist = self.session.query(model.Imagelist).\
                filter(model.Imagelist.id==private_id)
        return subscriptionlist
    
        # Now the virtual class
    def subscription_get(self,by_id):
        return self.subscription_by_id(private_id)
    
    
class queryby_uri(queryby_base):
    def subscription_get(self,url):
        return self.subscription_by_uri(url)

class queryby_uuid(queryby_base):
    def subscription_get(self,uuid):
        return self.subscription_by_uuid(uuid)


class output_driver_base:
    def __init__(self,file_pointer,session,anchor):
        self.session = session
        self.log = logging.getLogger("db_actions")
        self.file_pointer = file_pointer
        self.anchor = anchor
    def display_subscription_imagelist(self,subscription,imagelist):
        status = None
        
        self.display_subscription(subscription)
        self.display_imagelist(imagelist)
        
        return True
    def display_subscription(self,subscription):
        pass
    def display_imagelist(self,imagelist):
        pass
    def subscriptions_lister(self):
        pass

class output_driver_smime(output_driver_base):
    def display_subscription(self,subscription):
        pass
    def display_imagelist(self,imagelist):
        self.file_pointer.write (imagelist.data)

class output_driver_message(output_driver_base):
    def display_subscription(self,subscription):
        pass
    def display_imagelist(self,imagelist):
        
        validated_data = self.anchor.validate_text(str(imagelist.data))
        self.file_pointer.write (validated_data['data'])

class output_driver_lines(output_driver_base):
    def display_subscription(self,subscription):
        self.file_pointer.write ('subscription.dc:identifier=%s\n' % (subscription.uuid))
        self.file_pointer.write ('subscription.dc:description=%s\n' % (subscription.description))
        self.file_pointer.write ('subscription.sl:authorised=%s\n' % (subscription.authorised))
        self.file_pointer.write ('subscription.hv:uri=%s\n' % (subscription.url))
        if subscription.updated:
            self.file_pointer.write ('subscription.dc:date:updated=%s\n' % (subscription.updated.strftime(time_format_definition)))
        else:
            self.file_pointer.write ('subscription.dc:date:updated=%s\n'% (False))
        return True
    def display_imagelist(self,imagelist):
        
        validated_data = self.anchor.validate_text(str(imagelist.data))
        self.file_pointer.write (validated_data['data'])
    def display_imagelist(self,imagelist):
        self.file_pointer.write ('imagelist.dc:date:imported=%s\n' % (imagelist.imported.strftime(time_format_definition)))
        self.file_pointer.write ('imagelist.dc:date:created=%s\n' % (imagelist.created.strftime(time_format_definition)))
        self.file_pointer.write ('imagelist.dc:date:expires=%s\n' % (imagelist.expires.strftime(time_format_definition)))
        self.file_pointer.write ('imagelist.authorised=%s\n' % (imagelist.authorised))
    def subscriptions_lister(self):
        
        subauthq = self.session.query(model.Subscription).all()
        for item in subauthq:
            self.file_pointer.write ("%s\t%s\t%s\n" % (item.uuid,item.authorised,item.url))
            
class db_controler:
    def __init__(self,dboptions):
        self.log = logging.getLogger("db_controler")
        self.engine = create_engine(dboptions, echo=False)
        model.init(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)
        self.anchor = None
        self.factory_selector = None
        self.factory_view = None

    def setup_trust_anchor(self,directory):
        self.anchor = loadcanamespace.ViewTrustAnchor()
        self.anchor.update_ca_list(directory)
    def setup_selector_factory(self,factory):
        self.factory_selector = factory
    def setup_view_factory(self,factory):
        self.factory_view = factory
        
    # Utility functions
    def check_factories(self):
        if self.factory_view == None:
            self.log.warning("factory_view not available.")
            return False
        if self.factory_selector == None:
            self.log.warning("selector not available.")
            return False
        return True    
    def unsigned_message_by_identifier_tofilepath(self,instructions):
        
        
        Session = self.SessionFactory()
        db = db_actions(Session)
        for instruction in instructions:
            print instruction
        
        for selection_uuid in subscriptions_selected:
            db.sdsdsd(selection_uuid)
        Session.commit()
    def sessions_list(self):
        Session = self.SessionFactory()
        selector = self.factory_selector(Session)
        view = self.factory_view(sys.stdout,Session,self.anchor)
        view.subscriptions_lister()
        return True
    def subscriptions_update(self):
        if self.anchor == None:
            self.log.warning("No enabled certificates, check your x509 dir.")
            return False
        Session = self.SessionFactory()
        db = db_actions(Session)
        db.subscriptions_update(self.anchor)
        return True
    def subscriptions_delete(self,subscriptions_selected):
        Session = self.SessionFactory()
        db = db_actions(Session)
        for selection_uuid in subscriptions_selected:
            db.subscriptions_delete(selection_uuid)
        Session.commit()
        return True
    def subscriptions_subscribe(self,urls_selected):
        Session = self.SessionFactory()
        db = db_actions(Session)
        for uri in urls_selected:
            db.subscribe_file(self.anchor,uri)       
    
            
    def subscriptions_info(self,subscriptions_selected,outputfiles):
        if not self.check_factories():
            return False
        pairs, extra_selectors ,extra_paths = pairsNnot(subscriptions_selected,outputfiles)
        
        for item in extra_selectors:
            pairs.append([item,None])
            
        errorhappened = False
        Session = self.SessionFactory()
        selector = self.factory_selector(Session)
        for pair in pairs:
            selector_filter = pair[0]
            output_file_name = pair[1]        
            output_fileptr = sys.stdout
            if output_file_name != None:
                output_fileptr = open(output_file_name,'w+')
                output_fileptr.flush()
            
            query_subscription = selector.subscription_get(selector_filter)
            view = self.factory_view(output_fileptr,Session,self.anchor)
            
            for item in query_subscription:
                view.display_subscription(item)
                query_imagelist = selector.imagelist_by_id(item.imagelist_latest)
                for imagelist in query_imagelist:
                    view.display_imagelist(imagelist)
                    
            if output_file_name != None:
                output_fileptr.close()
                        

# User interface

def pairsNnot(list_a,list_b):
    len_generate_list = len(list_a)
    len_image_list = len(list_b)
    ocupies_generate_list = set(range(len_generate_list))
    ocupies_image_list = set(range(len_image_list))
    ocupies_pairs = ocupies_image_list.intersection(ocupies_generate_list)
    diff_a = ocupies_generate_list.difference(ocupies_image_list)
    diff_b = ocupies_image_list.difference(ocupies_generate_list)
    arepairs = []
    for i in ocupies_pairs:
        arepairs.append([list_a[i],list_b[i]])
    notpairs_a = []
    for i in diff_a:
        notpairs_a.append(list_a[i])
    notpairs_b = []
    for i in diff_b:
        notpairs_b.append(list_b[i])
    
    return arepairs,notpairs_a,notpairs_b


           
def main():
    log = logging.getLogger("main")
    """Runs program and handles command line options"""
    p = optparse.OptionParser(version = "%prog " + version)
    p.add_option('-l', '--list', action ='store_true',help='list subscriptions')
    p.add_option('-d', '--database', action ='store', help='Database Initiation string',
        default='sqlite:///tutorial.db')
    p.add_option('-s', '--subscribe', action ='append',help='Subscribe to URL', metavar='INPUTURL')
    p.add_option('-c', '--cert-dir', action ='store',help='Certificate directory.', metavar='INPUTDIR',
        default='/etc/grid-security/certificates/')
    p.add_option('-U', '--update', action ='store_true',help='update subscriptions')
    p.add_option('-u', '--uuid', action ='append',help='Select subscription', metavar='UUID')
    p.add_option('-r', '--uri', action ='append',help='Select subscription', metavar='URL')
    p.add_option('-f', '--format', action ='store',help='Sets teh output format')
    p.add_option('-D', '--delete', action ='store_true',help='Delete subscription')
    p.add_option('-i', '--info', action ='store_true',help='Information on subscription')
    p.add_option('-o', '--output', action ='append',help='Export File.', metavar='OUTPUTFILE')
    options, arguments = p.parse_args()
    anchor_needed = False
    format_needed = False
    actions = set([])
    subscriptions_selected = []
    subscription_url_list = []
    actionsrequiring_selections = set(['message','json','delete','info'])
    outputformats = set(['SMIME','message','lines'])
    output_format_selected = set([])
    inputformats = set(['uuid','url'])
    input_format_selected = set([])
    outputfiles = []
    if options.list:
        actions.add('list')
        output_format_selected.add('lines')
    if options.update:
        actions.add('update')
        anchor_needed = True
        output_format_selected.add('lines')
    if options.subscribe:
        anchor_needed = True
        actions.add('subscribe')
        subscription_url_list = options.subscribe
    if options.uuid:
        subscriptions_selected = options.uuid
        input_format_selected.add('uuid')
    if options.uri:
        subscriptions_selected = options.uri
        input_format_selected.add('url')
    
    if options.format:
        if options.format in outputformats:
            output_format_selected.add(options.format)
            anchor_needed = True
        else:
            log.error("Invalid format '%s' allowed formats are '%s'" % (options.format,outputformats))
            sys.exit(1)
    if options.delete:
        actions.add('delete')
    if options.info:
        format_needed = True
        actions.add('info')
    if options.output:
        format_needed = True
        outputfiles = options.output
    
    # 1 So we have some command line validation
    
    if len(actions) == 0:
        log.error("No actions selected")
        sys.exit(1)
    if len(actions) > 1:
        log.error("More than one action selected.")
        sys.exit(1)
    if format_needed and len(output_format_selected) == 0:
        log.error("No output format selected")
        sys.exit(1)
    
    # 1.1 Initate DB
    database = db_controler(options.database)
    
    # 2 Initate CA's to manage files
    if anchor_needed:
        database.setup_trust_anchor(options.cert_dir)
    
    # Handle conflicting actions
    actions_req_sel = actionsrequiring_selections.intersection(actions)
    
    actions_req_sel_len = len(actions_req_sel)
    if actions_req_sel_len == 1:
        
        if len(subscriptions_selected) == 0:
            log.error('No selections made.')
            sys.exit(1)
    if actions_req_sel_len > 1:
        log.error('Conflicting functions.')
        sys.exit(1)
    # Handle conflicting identifiers
    
    selectors_types = inputformats.intersection(input_format_selected)
    selectors_types_len = len(selectors_types)
    if selectors_types_len > 1:
        log.error('Conflicting selectors.')
        sys.exit(1)
    
    selector_str = 'uuid'
    
    if selectors_types_len == 1:
        selector_str = selectors_types.pop()
    
    mapper = {'uuid' : queryby_uuid,
            'url' : queryby_uri,
        }
        
    database.setup_selector_factory(mapper[selector_str])
    
    # Hnadler the output_view
    
    outputformats_selections = outputformats.intersection(output_format_selected)
    outputformats_selections_len = len(outputformats_selections)
    if outputformats_selections_len > 1:
        log.error('Conflicting output formats.')
        sys.exit(1)
    selector_str = 'lines'
    if outputformats_selections_len == 1:
        selector_str = outputformats_selections.pop()
    mapper = {'lines' : output_driver_lines,
        'SMIME' : output_driver_smime,
        'message' : output_driver_message,    
    }
    database.setup_view_factory(mapper[selector_str])
    
    if 'subscribe' in actions:
        database.subscriptions_subscribe(subscription_url_list)
    if 'list' in actions:
        database.sessions_list()
    if 'update' in actions:
        database.subscriptions_update()
    if 'delete' in actions:
        database.subscriptions_delete(subscriptions_selected)
    if 'dump' in actions:
        if not 'select' in actions:
            log.error('No subscriptions selected.')
        database.message_files(subscriptions_selected,outputfiles)
    if 'json' in actions:   
        database.dumpfiles(subscriptions_selected,outputfiles)
    if 'info' in actions:
        database.subscriptions_info(subscriptions_selected,outputfiles)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
