import logging
from string import Template

from cartography.util import aws_handle_regions
from cartography.util import run_cleanup_job
from cartography.util import timeit

logger = logging.getLogger(__name__)


def get_short_id_from_ec2_arn(arn):
    """
    Return the short-form resource ID from an EC2 ARN.
    For example, for "arn:aws:ec2:us-east-1:test_account:instance/i-1337", return 'i-1337'.
    :param arn: The ARN
    :return: The resource ID
    """
    return arn.split('/')[-1]


def get_bucket_name_from_arn(bucket_arn):
    """
    Return the bucket name from an S3 bucket ARN.
    For example, for "arn:aws:s3:::bucket_name", return 'bucket_name'.
    :param arn: The S3 bucket's full ARN
    :return: The S3 bucket's name
    """
    return bucket_arn.split(':')[-1]


# We maintain a mapping from AWS resource types to their associated labels and unique identifiers.
# label: the node label used in cartography for this resource type
# property: the field of this node that uniquely identified this resource type
# id_func: [optional] - EC2 instances and S3 buckets in cartography currently use non-ARNs as their primary identifiers
# so we need to supply a function pointer to translate the ARN returned by the resourcegroupstaggingapi to the form that
# cartography uses.
# TODO - we should make EC2 and S3 assets query-able by their full ARN so that we don't need this workaround.
TAG_RESOURCE_TYPE_MAPPINGS = {
    'ec2:instance': {'label': 'EC2Instance', 'property': 'id', 'id_func': get_short_id_from_ec2_arn},
    'ec2:network-interface': {'label': 'NetworkInterface', 'property': 'id', 'id_func': get_short_id_from_ec2_arn},
    'ec2:security-group': {'label': 'EC2SecurityGroup', 'property': 'id', 'id_func': get_short_id_from_ec2_arn},
    'ec2:subnet': {'label': 'EC2Subnet', 'property': 'subnetid', 'id_func': get_short_id_from_ec2_arn},
    'ec2:vpc': {'label': 'AWSVpc', 'property': 'id', 'id_func': get_short_id_from_ec2_arn},
    'ec2:transit-gateway': {'label': 'AWSTransitGateway', 'property': 'id'},
    'ec2:transit-gateway-attachment': {'label': 'AWSTransitGatewayAttachment', 'property': 'id'},
    'es:domain': {'label': 'ESDomain', 'property': 'id'},
    'redshift:cluster': {'label': 'RedshiftCluster', 'property': 'id'},
    'rds:db': {'label': 'RDSInstance', 'property': 'id'},
    'rds:subgrp': {'label': 'DBSubnetGroup', 'property': 'id'},
    # Buckets are the only objects in the S3 service: https://docs.aws.amazon.com/AmazonS3/latest/dev/s3-arn-format.html
    's3': {'label': 'S3Bucket', 'property': 'id', 'id_func': get_bucket_name_from_arn},
}


@timeit
@aws_handle_regions
def get_tags(boto3_session, resource_types, region):
    """
    Create boto3 client and retrieve tag data.
    """
    client = boto3_session.client('resourcegroupstaggingapi', region_name=region)
    paginator = client.get_paginator('get_resources')
    resources = []
    for page in paginator.paginate(
        # Only ingest tags for resources that Cartography supports.
        # This is just a starting list; there may be others supported by this API.
        ResourceTypeFilters=resource_types,
    ):
        resources.extend(page['ResourceTagMappingList'])
    return resources


@timeit
def load_tags(neo4j_session, tag_data, resource_type, region, aws_update_tag):
    INGEST_TAG_TEMPLATE = Template("""
    UNWIND $TagData as tag_mapping
        UNWIND tag_mapping.Tags as input_tag
            MATCH (resource:$resource_label{$property:tag_mapping.resource_id})
            MERGE(aws_tag:AWSTag:Tag{id:input_tag.Key + ":" + input_tag.Value})
            ON CREATE SET aws_tag.firstseen = timestamp()

            SET aws_tag.lastupdated = $UpdateTag,
            aws_tag.key = input_tag.Key,
            aws_tag.value =  input_tag.Value,
            aws_tag.region = $Region

            MERGE (resource)-[r:TAGGED]->(aws_tag)
            SET r.lastupdated = $UpdateTag,
            r.firstseen = timestamp()
    """)
    query = INGEST_TAG_TEMPLATE.safe_substitute(
        resource_label=TAG_RESOURCE_TYPE_MAPPINGS[resource_type]['label'],
        property=TAG_RESOURCE_TYPE_MAPPINGS[resource_type]['property'],
    )
    neo4j_session.run(
        query,
        TagData=tag_data,
        UpdateTag=aws_update_tag,
        Region=region,
    )


@timeit
def transform_tags(tag_data, resource_type):
    for tag_mapping in tag_data:
        tag_mapping['resource_id'] = compute_resource_id(tag_mapping, resource_type)


def compute_resource_id(tag_mapping, resource_type):
    resource_id = tag_mapping['ResourceARN']
    if 'id_func' in TAG_RESOURCE_TYPE_MAPPINGS[resource_type]:
        parse_resource_id_from_arn = TAG_RESOURCE_TYPE_MAPPINGS[resource_type]['id_func']
        resource_id = parse_resource_id_from_arn(tag_mapping['ResourceARN'])
    return resource_id


@timeit
def cleanup(neo4j_session, common_job_parameters):
    run_cleanup_job('aws_import_tags_cleanup.json', neo4j_session, common_job_parameters)


@timeit
def sync(neo4j_session, boto3_session, regions, aws_update_tag, common_job_parameters):
    for region in regions:
        logger.info("Syncing AWS tags for region '%s'.", region)
        for resource_type in TAG_RESOURCE_TYPE_MAPPINGS.keys():
            tag_data = get_tags(boto3_session, [resource_type], region)
            transform_tags(tag_data, resource_type)
            load_tags(neo4j_session, tag_data, resource_type, region, aws_update_tag)
    cleanup(neo4j_session, common_job_parameters)
