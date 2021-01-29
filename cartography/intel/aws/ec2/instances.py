import logging
import time

from .util import get_botocore_config
from cartography.util import aws_handle_regions
from cartography.util import run_cleanup_job
from cartography.util import timeit

logger = logging.getLogger(__name__)


@timeit
@aws_handle_regions
def get_ec2_instances(boto3_session, region):
    client = boto3_session.client('ec2', region_name=region, config=get_botocore_config())
    paginator = client.get_paginator('describe_instances')
    reservations = []
    for page in paginator.paginate():
        reservations.extend(page['Reservations'])
    return reservations


@timeit
def load_ec2_instance_network_interfaces(neo4j_session, instance_data, aws_update_tag):
    ingest_interfaces = """
    MATCH (instance:EC2Instance{instanceid: $InstanceId})
    UNWIND $Interfaces as interface
        MERGE (nic:NetworkInterface{id: interface.NetworkInterfaceId})
        ON CREATE SET nic.firstseen = timestamp()
        SET nic.status = interface.Status,
        nic.mac_address = interface.MacAddress,
        nic.description = interface.Description,
        nic.private_dns_name = interface.PrivateDnsName,
        nic.private_ip_address = interface.PrivateIpAddress,
        nic.lastupdated = $aws_update_tag

        MERGE (instance)-[r:NETWORK_INTERFACE]->(nic)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = $aws_update_tag

        WITH nic, interface
        WHERE interface.SubnetId IS NOT NULL
        MERGE (subnet:EC2Subnet{subnetid: interface.SubnetId})
        ON CREATE SET subnet.firstseen = timestamp()
        SET subnet.lastupdated = $aws_update_tag

        MERGE (nic)-[r:PART_OF_SUBNET]->(subnet)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = $aws_update_tag

        WITH nic, interface
        UNWIND interface.Groups as group
            MATCH (ec2group:EC2SecurityGroup{groupid: group.GroupId})
            MERGE (nic)-[r:MEMBER_OF_EC2_SECURITY_GROUP]->(ec2group)
            ON CREATE SET r.firstseen = timestamp()
            SET r.lastupdated = $aws_update_tag
    """
    instance_id = instance_data["InstanceId"]
    neo4j_session.run(
        ingest_interfaces,
        Interfaces=instance_data['NetworkInterfaces'],
        InstanceId=instance_id,
        aws_update_tag=aws_update_tag,
    ).consume()  # TODO see issue 170


@timeit
def load_ec2_instances(neo4j_session, data, region, current_aws_account_id, aws_update_tag):
    ingest_reservation = """
    MERGE (reservation:EC2Reservation{reservationid: $ReservationId})
    ON CREATE SET reservation.firstseen = timestamp()
    SET reservation.ownerid = $OwnerId, reservation.requesterid = $RequesterId, reservation.region = $Region,
    reservation.lastupdated = $aws_update_tag
    WITH reservation
    MATCH (awsAccount:AWSAccount{id: $AWS_ACCOUNT_ID})
    MERGE (awsAccount)-[r:RESOURCE]->(reservation)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    """

    ingest_instance = """
    MERGE (instance:Instance:EC2Instance{id: $InstanceId})
    ON CREATE SET instance.firstseen = timestamp()
    SET instance.instanceid = $InstanceId, instance.publicdnsname = $PublicDnsName,
    instance.privateipaddress = $PrivateIpAddress, instance.publicipaddress = $PublicIpAddress,
    instance.imageid = $ImageId, instance.instancetype = $InstanceType, instance.monitoringstate = $MonitoringState,
    instance.state = $State, instance.launchtime = $LaunchTime, instance.launchtimeunix = $LaunchTimeUnix,
    instance.region = $Region, instance.lastupdated = $aws_update_tag,
    instance.iaminstanceprofile = $IamInstanceProfile
    WITH instance
    MATCH (rez:EC2Reservation{reservationid: $ReservationId})
    MERGE (instance)-[r:MEMBER_OF_EC2_RESERVATION]->(rez)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    WITH instance
    MATCH (aa:AWSAccount{id: $AWS_ACCOUNT_ID})
    MERGE (aa)-[r:RESOURCE]->(instance)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    """

    ingest_subnet = """
    MATCH (instance:EC2Instance{id: $InstanceId})
    MERGE (subnet:EC2Subnet{subnetid: $SubnetId})
    ON CREATE SET subnet.firstseen = timestamp()
    SET subnet.region = $Region,
    subnet.lastupdated = $aws_update_tag
    MERGE (instance)-[r:PART_OF_SUBNET]->(subnet)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    """

    ingest_key_pair = """
    MERGE (keypair:KeyPair:EC2KeyPair{arn: $KeyPairARN, id: $KeyPairARN})
    ON CREATE SET keypair.firstseen = timestamp()
    SET keypair.keyname = $KeyName, keypair.region = $Region, keypair.lastupdated = $aws_update_tag
    WITH keypair
    MATCH (aa:AWSAccount{id: $AWS_ACCOUNT_ID})
    MERGE (aa)-[r:RESOURCE]->(keypair)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    with keypair
    MATCH (instance:EC2Instance{instanceid: $InstanceId})
    MERGE (instance)<-[r:SSH_LOGIN_TO]-(keypair)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    """

    ingest_security_groups = """
    MERGE (group:EC2SecurityGroup{id: $GroupId})
    ON CREATE SET group.firstseen = timestamp(), group.groupid = $GroupId
    SET group.name = $GroupName, group.region = $Region, group.lastupdated = $aws_update_tag
    WITH group
    MATCH (aa:AWSAccount{id: $AWS_ACCOUNT_ID})
    MERGE (aa)-[r:RESOURCE]->(group)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    WITH group
    MATCH (instance:EC2Instance{instanceid: $InstanceId})
    MERGE (instance)-[r:MEMBER_OF_EC2_SECURITY_GROUP]->(group)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = $aws_update_tag
    """

    for reservation in data:
        reservation_id = reservation["ReservationId"]

        neo4j_session.run(
            ingest_reservation,
            ReservationId=reservation_id,
            OwnerId=reservation.get("OwnerId"),
            RequesterId=reservation.get("RequesterId"),
            AWS_ACCOUNT_ID=current_aws_account_id,
            Region=region,
            aws_update_tag=aws_update_tag,
        ).consume()  # TODO see issue 170

        for instance in reservation["Instances"]:
            instanceid = instance["InstanceId"]

            monitoring_state = instance.get("Monitoring", {}).get("State")

            instance_state = instance.get("State", {}).get("Name")

            # NOTE this is a hack because we're using a version of Neo4j that doesn't support temporal data types
            launch_time = instance.get("LaunchTime")
            if launch_time:
                launch_time_unix = time.mktime(launch_time.timetuple())
            else:
                launch_time_unix = ""

            neo4j_session.run(
                ingest_instance,
                InstanceId=instanceid,
                PublicDnsName=instance.get("PublicDnsName"),
                PublicIpAddress=instance.get("PublicIpAddress"),
                PrivateIpAddress=instance.get("PrivateIpAddress"),
                ImageId=instance.get("ImageId"),
                InstanceType=instance.get("InstanceType"),
                IamInstanceProfile=instance.get("IamInstanceProfile", {}).get("Arn"),
                ReservationId=reservation_id,
                MonitoringState=monitoring_state,
                LaunchTime=str(launch_time),
                LaunchTimeUnix=launch_time_unix,
                State=instance_state,
                AWS_ACCOUNT_ID=current_aws_account_id,
                Region=region,
                aws_update_tag=aws_update_tag,
            ).consume()  # TODO see issue 170

            # SubnetId can return None intermittently so attach only if non-None.
            subnet_id = instance.get('SubnetId')
            if subnet_id:
                neo4j_session.run(
                    ingest_subnet,
                    InstanceId=instanceid,
                    SubnetId=subnet_id,
                    Region=region,
                    aws_update_tag=aws_update_tag,
                )

            if instance.get("KeyName"):
                key_name = instance["KeyName"]
                key_pair_arn = f'arn:aws:ec2:{region}:{current_aws_account_id}:key-pair/{key_name}'
                neo4j_session.run(
                    ingest_key_pair,
                    KeyPairARN=key_pair_arn,
                    KeyName=key_name,
                    Region=region,
                    InstanceId=instanceid,
                    AWS_ACCOUNT_ID=current_aws_account_id,
                    aws_update_tag=aws_update_tag,
                ).consume()  # TODO see issue 170

            if instance.get("SecurityGroups"):
                for group in instance["SecurityGroups"]:
                    neo4j_session.run(
                        ingest_security_groups,
                        GroupId=group["GroupId"],
                        GroupName=group.get("GroupName"),
                        InstanceId=instanceid,
                        Region=region,
                        AWS_ACCOUNT_ID=current_aws_account_id,
                        aws_update_tag=aws_update_tag,
                    ).consume()  # TODO see issue 170

            load_ec2_instance_network_interfaces(neo4j_session, instance, aws_update_tag)


@timeit
def cleanup_ec2_instances(neo4j_session, common_job_parameters):
    run_cleanup_job('aws_import_ec2_instances_cleanup.json', neo4j_session, common_job_parameters)


@timeit
def sync_ec2_instances(
    neo4j_session, boto3_session, regions, current_aws_account_id, aws_update_tag,
    common_job_parameters,
):
    for region in regions:
        logger.info("Syncing EC2 instances for region '%s' in account '%s'.", region, current_aws_account_id)
        data = get_ec2_instances(boto3_session, region)
        load_ec2_instances(neo4j_session, data, region, current_aws_account_id, aws_update_tag)
    cleanup_ec2_instances(neo4j_session, common_job_parameters)
