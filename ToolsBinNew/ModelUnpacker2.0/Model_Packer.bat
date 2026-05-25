@echo off
cd /d %~dp0
for %%s in (%*) do Model_Packer d3d -inplace %%s -verbose
